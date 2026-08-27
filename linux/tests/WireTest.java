import java.io.DataInputStream;
import java.io.FileInputStream;

/**
 * Lee la muestra binaria que genera test_pipeline.py con exactamente la misma
 * secuencia de llamadas que StreamClient. Si el orden o el endianness del lado
 * Python no coincidieran, aquí saldrían valores basura.
 */
public class WireTest {

    public static void main(String[] args) throws Exception {
        DataInputStream in = new DataInputStream(
                new FileInputStream("/tmp/wire-sample.bin"));

        byte[] magic = new byte[4];
        in.readFully(magic);
        expect("magic", new String(magic, "US-ASCII"), "BSCR");

        // Y, sobre todo, que la constante que compila DE VERDAD la app Android
        // sea esa misma. Al renombrar el proyecto, el `sed` no vio esta
        // constante porque está escrita carácter a carácter, y sin esta
        // comprobación la tablet y el PC habrían dejado de entenderse sin que
        // ninguna prueba dijera nada.
        expect("la constante MAGIC de la app Android coincide",
                new String(cl.danko.bothscreen.Protocol.MAGIC, "US-ASCII"),
                new String(magic, "US-ASCII"));
        expect("y la versión del protocolo también",
                cl.danko.bothscreen.Protocol.VERSION, 1);
        expect("version", in.readInt(), 1);
        expect("codec HEVC", in.readInt(), 1);
        expect("ancho", in.readInt(), 1920);
        expect("alto", in.readInt(), 1200);
        expect("fps", in.readInt(), 60);
        expect("bitrate", in.readInt(), 8000);

        expect("tipo FRAME", in.readUnsignedByte(), 0);
        expect("flags keyframe", in.readUnsignedByte(), 2);
        expect("pts", in.readLong(), 123456789L);
        int size = in.readInt();
        expect("tamaño", size, 8);
        byte[] payload = new byte[size];
        in.readFully(payload);
        expect("primer byte del NAL", payload[3] & 0xff, 1);

        expect("tipo STATS", in.readUnsignedByte(), 1);
        expect("bitrate stats", in.readInt(), 7250);
        expect("fps x10", in.readInt(), 584);

        expect("fin del archivo", in.read(), -1);
        in.close();

        if (failures == 0) {
            System.out.println("  El lector Java interpreta el formato Python sin errores");
        } else {
            System.out.println("  " + failures + " discrepancias");
            System.exit(1);
        }
    }

    static int failures = 0;

    static void expect(String what, Object got, Object want) {
        boolean ok = got.equals(want);
        if (!ok) {
            failures++;
        }
        System.out.println("  [" + (ok ? "OK  " : "FALLA") + "] " + what
                + " = " + got + (ok ? "" : " (esperaba " + want + ")"));
    }
}
