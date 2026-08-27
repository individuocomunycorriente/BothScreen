package cl.danko.bothscreen;

/**
 * Constantes del protocolo compartido con el daemon de Linux.
 *
 * Todo va en big-endian, que es justo lo que hacen DataInputStream y
 * DataOutputStream sin configuración extra.
 */
public final class Protocol {

    private Protocol() {
    }

    public static final byte[] MAGIC = {'B', 'S', 'C', 'R'};
    public static final int VERSION = 1;

    public static final int DEFAULT_PORT = 27183;

    public static final int CODEC_H264 = 0;
    public static final int CODEC_HEVC = 1;

    public static final int CODEC_BIT_H264 = 1;
    public static final int CODEC_BIT_HEVC = 2;

    public static final int MSG_FRAME = 0;
    public static final int MSG_STATS = 1;

    public static final byte CTL_ACK = 1;
    public static final byte CTL_REQUEST_KEYFRAME = 2;

    public static final int FLAG_CONFIG = 1;
    public static final int FLAG_KEYFRAME = 2;

    public static String mimeFor(int codec) {
        return codec == CODEC_HEVC ? "video/hevc" : "video/avc";
    }

    public static String nameFor(int codec) {
        return codec == CODEC_HEVC ? "HEVC" : "H.264";
    }
}
