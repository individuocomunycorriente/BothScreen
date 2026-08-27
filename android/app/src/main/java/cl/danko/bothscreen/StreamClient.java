package cl.danko.bothscreen;

import android.util.Log;
import android.view.Surface;

import java.io.BufferedInputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.Socket;

/**
 * Hilo de red: se conecta al PC por el túnel USB y alimenta al decodificador.
 *
 * La dirección siempre es 127.0.0.1 porque el daemon crea un `adb reverse`: el
 * puerto local de la tablet queda cableado al puerto del PC a través del cable
 * USB-C. No hay Wi-Fi de por medio ni descubrimiento de red que pueda fallar.
 */
public class StreamClient extends Thread implements VideoDecoder.Listener {

    private static final String TAG = "BothScreen";
    // Reintento con espera creciente: si el PC no está, no tiene sentido
    // martillear el socket cada 800 ms durante horas gastando batería.
    private static final int RECONNECT_MIN_MS = 800;
    private static final int RECONNECT_MAX_MS = 5000;
    private static final int SOCKET_BUFFER = 1 << 20;

    public interface Listener {
        void onStatus(String status);

        void onStreamStarted(int codec, int width, int height, int fps);

        void onStats(int bitrateKbps, float fps, long framesDecoded);

        void onStreamStopped();
    }

    private final Surface surface;
    private final int port;
    private final int panelWidth;
    private final int panelHeight;
    private final int maxFps;
    private final Listener listener;

    private volatile boolean running = true;
    private Socket socket;
    private DataOutputStream out;
    private VideoDecoder decoder;

    private final Object writeLock = new Object();

    public StreamClient(Surface surface, int port, int panelWidth, int panelHeight,
                        int maxFps, Listener listener) {
        super("stream-client");
        this.surface = surface;
        this.port = port;
        this.panelWidth = panelWidth;
        this.panelHeight = panelHeight;
        this.maxFps = maxFps;
        this.listener = listener;
    }

    @Override
    public void run() {
        int delay = RECONNECT_MIN_MS;
        while (running) {
            try {
                connectAndStream();
                delay = RECONNECT_MIN_MS;
            } catch (IOException e) {
                if (running) {
                    Log.i(TAG, "conexión perdida: " + e.getMessage());
                    status("Esperando al PC…");
                }
            } catch (Exception e) {
                Log.e(TAG, "error inesperado", e);
                status("Error: " + e.getMessage());
            } finally {
                cleanup();
            }
            if (running) {
                try {
                    Thread.sleep(delay);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
                delay = Math.min(delay * 2, RECONNECT_MAX_MS);
            }
        }
        Log.i(TAG, "hilo de red terminado");
    }

    private void connectAndStream() throws Exception {
        status("Conectando por USB…");
        socket = new Socket();
        socket.connect(new InetSocketAddress("127.0.0.1", port), 3000);
        socket.setTcpNoDelay(true);
        socket.setReceiveBufferSize(SOCKET_BUFFER);
        socket.setSoTimeout(15000);

        DataInputStream in = new DataInputStream(
                new BufferedInputStream(socket.getInputStream(), SOCKET_BUFFER));
        out = new DataOutputStream(socket.getOutputStream());

        sendHello();

        byte[] magic = new byte[4];
        in.readFully(magic);
        for (int i = 0; i < 4; i++) {
            if (magic[i] != Protocol.MAGIC[i]) {
                throw new IOException("respuesta con magic inválido");
            }
        }
        in.readInt();                       // versión del protocolo
        int codec = in.readInt();
        int width = in.readInt();
        int height = in.readInt();
        int fps = in.readInt();
        int bitrate = in.readInt();

        Log.i(TAG, "config: " + Protocol.nameFor(codec) + " " + width + "x" + height
                + "@" + fps + " " + bitrate + " kbps");

        decoder = new VideoDecoder(surface, this);
        decoder.start(codec, width, height, fps);

        if (listener != null) {
            listener.onStreamStarted(codec, width, height, fps);
        }
        status("");

        readLoop(in);
    }

    private void sendHello() throws IOException {
        byte[] name = android.os.Build.MODEL.getBytes("UTF-8");
        DataOutputStream hello = out;
        synchronized (writeLock) {
            hello.write(Protocol.MAGIC);
            hello.writeInt(Protocol.VERSION);
            hello.writeInt(panelWidth);
            hello.writeInt(panelHeight);
            hello.writeInt(maxFps);
            hello.writeInt(VideoDecoder.supportedCodecs());
            hello.writeInt(name.length);
            hello.write(name);
            hello.flush();
        }
    }

    private void readLoop(DataInputStream in) throws IOException {
        byte[] payload = new byte[1 << 20];
        while (running) {
            int type = in.readUnsignedByte();
            if (type == Protocol.MSG_FRAME) {
                int flags = in.readUnsignedByte();
                long ptsUs = in.readLong();
                int size = in.readInt();
                if (size < 0 || size > (16 << 20)) {
                    throw new IOException("tamaño de frame absurdo: " + size);
                }
                if (size > payload.length) {
                    payload = new byte[size];
                }
                in.readFully(payload, 0, size);

                boolean isConfig = (flags & Protocol.FLAG_CONFIG) != 0;
                if (decoder != null) {
                    boolean ok = decoder.feed(payload, size, ptsUs, isConfig);
                    if (!ok && !isConfig) {
                        // El decodificador se quedó sin buffers: pide una imagen
                        // completa para volver a engancharse sin arrastrar
                        // artefactos.
                        requestKeyframe();
                    }
                }
            } else if (type == Protocol.MSG_STATS) {
                int bitrate = in.readInt();
                int fpsX10 = in.readInt();
                if (listener != null) {
                    listener.onStats(bitrate, fpsX10 / 10f,
                            decoder != null ? decoder.framesDecoded() : 0);
                }
            } else {
                throw new IOException("mensaje desconocido: " + type);
            }
        }
    }

    @Override
    public void onFramePresented(long ptsUs) {
        // Esto llega desde el hilo de salida del decodificador, que puede
        // seguir vivo un instante después de que cleanup() ponga `out` a null.
        // Por eso se copia la referencia antes de usarla.
        DataOutputStream stream = out;
        if (stream == null) {
            return;
        }
        try {
            synchronized (writeLock) {
                stream.writeByte(Protocol.CTL_ACK);
                stream.writeLong(ptsUs);
                stream.flush();
            }
        } catch (IOException e) {
            Log.d(TAG, "no se pudo enviar ACK: " + e.getMessage());
        }
    }

    @Override
    public void onDecoderError(String message) {
        status("Decodificador: " + message);
        closeSocket();
    }

    private void requestKeyframe() {
        DataOutputStream stream = out;
        if (stream == null) {
            return;
        }
        try {
            synchronized (writeLock) {
                stream.writeByte(Protocol.CTL_REQUEST_KEYFRAME);
                stream.flush();
            }
        } catch (IOException ignored) {
        }
    }

    private void status(String text) {
        if (listener != null) {
            listener.onStatus(text);
        }
    }

    private void cleanup() {
        if (decoder != null) {
            decoder.stop();
            decoder = null;
        }
        closeSocket();
        out = null;
        if (listener != null) {
            listener.onStreamStopped();
        }
    }

    private void closeSocket() {
        if (socket != null) {
            try {
                socket.close();
            } catch (IOException ignored) {
            }
            socket = null;
        }
    }

    public void shutdown() {
        running = false;
        closeSocket();
        interrupt();
    }
}
