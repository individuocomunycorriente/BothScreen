package cl.danko.bothscreen;

import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaCodecList;
import android.media.MediaFormat;
import android.os.Build;
import android.util.Log;
import android.view.Surface;

import java.nio.ByteBuffer;

/**
 * Decodificador por hardware que pinta directo sobre el Surface.
 *
 * Se usa el modo sincrónico a propósito: el hilo de red mete las unidades de
 * acceso a medida que llegan y un hilo aparte saca los frames y los presenta de
 * inmediato con releaseOutputBuffer(index, true). Nada de reordenar por
 * timestamp ni de esperar al reloj de presentación: en una segunda pantalla lo
 * que importa es que el pixel salga cuanto antes.
 */
public class VideoDecoder {

    private static final String TAG = "BothScreen";

    public interface Listener {
        /** Llamado cuando un frame ya se presentó, para confirmar al PC. */
        void onFramePresented(long ptsUs);

        void onDecoderError(String message);
    }

    private final Surface surface;
    private final Listener listener;

    private MediaCodec codec;
    private Thread outputThread;
    private volatile boolean running;
    private volatile boolean sawConfig;

    private long framesDecoded;

    public VideoDecoder(Surface surface, Listener listener) {
        this.surface = surface;
        this.listener = listener;
    }

    /** Máscara de códecs que este dispositivo puede decodificar por hardware. */
    public static int supportedCodecs() {
        int mask = 0;
        if (hasDecoder("video/avc")) {
            mask |= Protocol.CODEC_BIT_H264;
        }
        if (hasDecoder("video/hevc")) {
            mask |= Protocol.CODEC_BIT_HEVC;
        }
        return mask;
    }

    private static boolean hasDecoder(String mime) {
        MediaCodecList list = new MediaCodecList(MediaCodecList.REGULAR_CODECS);
        for (MediaCodecInfo info : list.getCodecInfos()) {
            if (info.isEncoder()) {
                continue;
            }
            for (String type : info.getSupportedTypes()) {
                if (type.equalsIgnoreCase(mime)) {
                    return true;
                }
            }
        }
        return false;
    }

    public void start(int codecId, int width, int height, int fps) throws Exception {
        String mime = Protocol.mimeFor(codecId);
        MediaFormat format = MediaFormat.createVideoFormat(mime, width, height);
        format.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, width * height);

        // Claves por nombre en vez de por constante: así el mismo código compila
        // contra un android.jar antiguo y sigue activando el modo de baja
        // latencia en Android 11+, donde el framework sí las lee.
        format.setInteger("low-latency", 1);       // KEY_LOW_LATENCY (API 30)
        format.setInteger("priority", 0);          // 0 = tiempo real
        format.setInteger("operating-rate", fps);  // pista para el gobernador
        if (Build.VERSION.SDK_INT >= 23) {
            format.setInteger("vendor.qti-ext-dec-low-latency.enable", 1);
        }

        codec = MediaCodec.createDecoderByType(mime);
        try {
            codec.configure(format, surface, null, 0);
        } catch (IllegalArgumentException e) {
            // Algún fabricante rechaza las claves de vendor; reintenta limpio.
            Log.w(TAG, "configure falló con claves extra, reintento: " + e);
            codec.release();
            format = MediaFormat.createVideoFormat(mime, width, height);
            format.setInteger("low-latency", 1);
            codec = MediaCodec.createDecoderByType(mime);
            codec.configure(format, surface, null, 0);
        }
        codec.start();

        running = true;
        sawConfig = false;
        framesDecoded = 0;

        outputThread = new Thread(new Runnable() {
            @Override
            public void run() {
                drainOutput();
            }
        }, "decoder-output");
        outputThread.start();
        Log.i(TAG, "decodificador listo: " + mime + " " + width + "x" + height);
    }

    /** Encola una unidad de acceso Annex-B. Bloquea si no hay buffers libres. */
    public boolean feed(byte[] data, int length, long ptsUs, boolean isConfig) {
        if (!running || codec == null) {
            return false;
        }
        int index;
        try {
            index = codec.dequeueInputBuffer(200000L);
        } catch (IllegalStateException e) {
            fail("dequeueInputBuffer: " + e.getMessage());
            return false;
        }
        if (index < 0) {
            // Sin buffers en 200 ms: el decodificador está atascado. Mejor
            // soltar el frame que acumular latencia.
            return false;
        }
        ByteBuffer buffer;
        if (Build.VERSION.SDK_INT >= 21) {
            buffer = codec.getInputBuffer(index);
        } else {
            buffer = codec.getInputBuffers()[index];
        }
        if (buffer == null) {
            return false;
        }
        buffer.clear();
        if (buffer.capacity() < length) {
            Log.w(TAG, "buffer de entrada demasiado chico (" + buffer.capacity()
                    + " < " + length + ")");
            codec.queueInputBuffer(index, 0, 0, ptsUs, 0);
            return false;
        }
        buffer.put(data, 0, length);

        int flags = 0;
        if (isConfig) {
            flags |= MediaCodec.BUFFER_FLAG_CODEC_CONFIG;
            sawConfig = true;
        }
        try {
            codec.queueInputBuffer(index, 0, length, ptsUs, flags);
        } catch (IllegalStateException e) {
            fail("queueInputBuffer: " + e.getMessage());
            return false;
        }
        return true;
    }

    public boolean hasConfig() {
        return sawConfig;
    }

    public long framesDecoded() {
        return framesDecoded;
    }

    private void drainOutput() {
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        while (running) {
            int index;
            try {
                index = codec.dequeueOutputBuffer(info, 20000L);
            } catch (IllegalStateException e) {
                if (running) {
                    fail("dequeueOutputBuffer: " + e.getMessage());
                }
                return;
            }
            if (index >= 0) {
                boolean render = (info.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0
                        && info.size > 0;
                try {
                    codec.releaseOutputBuffer(index, render);
                } catch (IllegalStateException e) {
                    if (running) {
                        fail("releaseOutputBuffer: " + e.getMessage());
                    }
                    return;
                }
                if (render) {
                    framesDecoded++;
                    if (listener != null) {
                        listener.onFramePresented(info.presentationTimeUs);
                    }
                }
            } else if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                Log.i(TAG, "formato de salida: " + codec.getOutputFormat());
            }
        }
    }

    private void fail(String message) {
        Log.e(TAG, message);
        running = false;
        if (listener != null) {
            listener.onDecoderError(message);
        }
    }

    public void stop() {
        running = false;
        if (outputThread != null) {
            try {
                outputThread.join(500);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
            outputThread = null;
        }
        if (codec != null) {
            try {
                codec.stop();
            } catch (Exception ignored) {
                // el codec puede estar ya en error
            }
            try {
                codec.release();
            } catch (Exception ignored) {
            }
            codec = null;
        }
    }
}
