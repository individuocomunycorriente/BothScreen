"""Prueba de extremo a extremo del servidor con una tablet simulada.

Se sustituyen las dos únicas piezas que necesitan hardware/GNOME real (el
monitor virtual de Mutter y el codificador VA-API) por dobles; todo lo demás
—handshake, negociación, cola de envío, ACKs, control adaptativo y el formato
de los mensajes— es el código de producción.
"""

import socket
import struct
import sys
import threading
import time

sys.path.insert(0, "..")

from bothscreen import encoder, mainloop, protocol, server  # noqa: E402
from test_pipeline import TestStreamer, check  # noqa: E402


class FakeMonitor:
    instances = []

    def __init__(self, width, height, refresh_rate=60.0, cursor_mode=1,
                 is_platform=True):
        self.width, self.height, self.refresh_rate = width, height, refresh_rate
        self.stopped = False
        FakeMonitor.instances.append(self)

    def start(self, timeout_ms=8000):
        return 0

    def stop(self):
        self.stopped = True


class FakeTablet(threading.Thread):
    """Habla el protocolo exactamente como lo hace StreamClient en Kotlin/Java."""

    def __init__(self, port, ack=True):
        super().__init__(daemon=True)
        self.port = port
        self.ack = ack
        self.config = None
        self.frames = []
        self.stats = []
        self.error = None
        self.running = True
        self.closed = False

    def run(self):
        try:
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            name = b"SM-T870"
            sock.sendall(protocol.HELLO_FIXED.pack(
                protocol.MAGIC, protocol.PROTO_VERSION, 2560, 1600, 120,
                protocol.CODEC_BIT_H264 | protocol.CODEC_BIT_HEVC, len(name))
                + name)

            fh = sock.makefile("rb")
            self.config = protocol.CONFIG_STRUCT.unpack(
                fh.read(protocol.CONFIG_STRUCT.size))

            while self.running:
                kind = fh.read(1)
                if not kind:
                    break
                if kind[0] == protocol.MSG_FRAME:
                    head = fh.read(protocol.FRAME_HEADER.size - 1)
                    flags, pts, size = struct.unpack(">BQI", head)
                    payload = fh.read(size)
                    if len(payload) != size:
                        break
                    self.frames.append((flags, pts, payload))
                    if self.ack:
                        sock.sendall(struct.pack(">BQ", protocol.CTL_ACK, pts))
                elif kind[0] == protocol.MSG_STATS:
                    bitrate, fps10 = struct.unpack(">II",
                                                   fh.read(8))
                    self.stats.append((bitrate, fps10 / 10.0))
                else:
                    self.error = "mensaje desconocido %d" % kind[0]
                    break
            self.closed = True
            sock.close()
        except Exception as exc:  # pragma: no cover
            self.closed = True
            self.error = str(exc)

    def stop(self):
        self.running = False


def main():
    mainloop.start()
    ok = True

    server.VirtualMonitor = FakeMonitor
    encoder.Streamer = TestStreamer

    cfg = server.Config()
    cfg.port = 28999
    cfg.fps = 30
    cfg.start_bitrate = 4000
    cfg.max_bitrate = 12000
    srv = server.DisplayServer(cfg)
    srv.start()

    print("\n1) Handshake y negociación")
    tablet = FakeTablet(cfg.port)
    tablet.start()
    time.sleep(3.5)

    ok &= check("la tablet recibió CONFIG", tablet.config is not None)
    if tablet.config:
        magic, version, codec, w, h, fps, bitrate = tablet.config
        ok &= check("magic correcto", magic == protocol.MAGIC)
        ok &= check("versión 1", version == protocol.PROTO_VERSION)
        ok &= check("resolución 1920x1200", (w, h) == (1920, 1200),
                    "%dx%d" % (w, h))
        ok &= check("fps limitado a 30", fps == 30, str(fps))
        ok &= check("bitrate inicial", bitrate == 4000, str(bitrate))

    ok &= check("el monitor virtual se creó con esa resolución",
                FakeMonitor.instances and
                (FakeMonitor.instances[0].width,
                 FakeMonitor.instances[0].height) == (1920, 1200))

    print("\n2) Flujo de vídeo")
    ok &= check("llegaron frames", len(tablet.frames) > 15,
                "%d" % len(tablet.frames))
    if tablet.frames:
        ok &= check("el primero es la CSD",
                    tablet.frames[0][0] == protocol.FLAG_CONFIG,
                    "flags=%d, %d bytes" % (tablet.frames[0][0],
                                            len(tablet.frames[0][2])))
        ok &= check("el segundo es keyframe",
                    tablet.frames[1][0] & protocol.FLAG_KEYFRAME)
        ok &= check("ningún frame vacío",
                    all(len(f[2]) > 0 for f in tablet.frames))
    ok &= check("llegaron estadísticas", len(tablet.stats) >= 3,
                "%d mensajes" % len(tablet.stats))
    if tablet.stats:
        ok &= check("el caudal medido es plausible",
                    0 < tablet.stats[-1][0] < 60000,
                    "%.0f kbps" % tablet.stats[-1][0])

    session = srv.session
    ok &= check("los ACK llegan al servidor",
                session._frames_acked > 10,
                "%d de %d" % (session._frames_acked, session._frames_out))
    ok &= check("con ACKs al día el adaptador sube el bitrate",
                session.bitrate > 4000, "%d kbps" % session.bitrate)
    ok &= check("no se disparó congestión",
                session._stats["in_flight"] <= 4,
                "%d en vuelo" % session._stats["in_flight"])

    print("\n3) Desconexión")
    tablet.stop()
    time.sleep(1.5)
    ok &= check("la sesión se cerró sola", not session.running)
    ok &= check("el monitor virtual se retiró",
                FakeMonitor.instances[0].stopped)

    print("\n4) Reconexión")
    tablet2 = FakeTablet(cfg.port)
    tablet2.start()
    time.sleep(2.5)
    ok &= check("la segunda conexión también arranca",
                tablet2.config is not None and len(tablet2.frames) > 5,
                "%d frames" % len(tablet2.frames))
    ok &= check("se creó un monitor virtual nuevo",
                len(FakeMonitor.instances) == 2)
    tablet2.stop()
    time.sleep(0.5)

    srv.stop()
    mainloop.stop()
    print("\n%s" % ("TODAS LAS PRUEBAS PASARON" if ok else "HAY FALLOS"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
