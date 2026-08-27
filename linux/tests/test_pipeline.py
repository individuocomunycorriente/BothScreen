"""Prueba el camino de codificación y el empaquetado sin necesitar GNOME.

Sustituye pipewiresrc por videotestsrc, que genera movimiento real, y comprueba
que salen unidades de acceso Annex-B con la cabecera de configuración primero,
keyframes periódicos, y que los ajustes en caliente no rompen el pipeline.
"""

import sys
import time

sys.path.insert(0, "..")

from bothscreen import encoder, mainloop, protocol  # noqa: E402
from bothscreen.server import Config, negotiate_resolution, _Sender  # noqa: E402


class TestStreamer(encoder.Streamer):
    def _candidates(self):
        parse = "h264parse" if self.codec == protocol.CODEC_H264 else "h265parse"
        caps = ("video/x-h264" if self.codec == protocol.CODEC_H264
                else "video/x-h265")
        enc = "x264enc" if self.codec == protocol.CODEC_H264 else "x265enc"
        return [(
            "test", False, enc,
            "videotestsrc pattern=smpte is-live=true ! "
            "video/x-raw,width={w},height={h},framerate={fps}/1 ! "
            "queue name=q max-size-buffers=5 ! "
            "videoconvertscale ! video/x-raw,format=I420 ! "
            "{enc} name=venc ! {parse} name=parse config-interval=-1 ! "
            "{caps},stream-format=byte-stream,alignment=au ! "
            "appsink name=sink emit-signals=true sync=false max-buffers=4 "
            "drop=false".format(w=self.width, h=self.height, fps=self.fps,
                                enc=enc, parse=parse, caps=caps)
        )]


def check(name, condition, detail=""):
    mark = "OK  " if condition else "FALLA"
    print("  [%s] %s %s" % (mark, name, detail))
    return bool(condition)


def main():
    mainloop.start()
    ok = True

    print("\n1) Negociación de resolución")
    cfg = Config()
    ok &= check("Tab S7 2560x1600 -> 1920x1200",
                negotiate_resolution(cfg, 2560, 1600) == (1920, 1200),
                str(negotiate_resolution(cfg, 2560, 1600)))
    ok &= check("panel 16:9 -> 1920x1080",
                negotiate_resolution(cfg, 1920, 1080) == (1920, 1080))
    ok &= check("panel en vertical se corrige",
                negotiate_resolution(cfg, 1600, 2560) == (1920, 1200))
    ok &= check("panel pequeño no se amplía",
                negotiate_resolution(cfg, 1280, 800) == (1280, 800))

    print("\n2) Pipeline de codificación (H.264 software)")
    frames = []

    def on_frame(flags, pts_us, data):
        frames.append((flags, pts_us, data))

    st = TestStreamer(0, 1280, 800, 30, protocol.CODEC_H264, 4000,
                      on_frame=on_frame)
    st.start()
    time.sleep(3.0)

    ok &= check("llegaron frames", len(frames) > 20, "%d frames" % len(frames))
    if frames:
        first_flags = frames[0][0]
        ok &= check("el primer paquete es solo configuración (SPS/PPS)",
                    first_flags == protocol.FLAG_CONFIG,
                    "flags=%d, %d bytes" % (first_flags, len(frames[0][2])))
        ok &= check("la CSD es pequeña", len(frames[0][2]) < 200,
                    "%d bytes" % len(frames[0][2]))
        ok &= check("el segundo paquete es el keyframe",
                    frames[1][0] & protocol.FLAG_KEYFRAME and
                    not (frames[1][0] & protocol.FLAG_CONFIG))
        cfg_nals = encoder.split_parameter_sets(frames[1][2],
                                                protocol.CODEC_H264)[0]
        ok &= check("el keyframe ya no lleva SPS/PPS delante", cfg_nals is None)
        keys = sum(1 for f, _, _ in frames if f & protocol.FLAG_KEYFRAME)
        ok &= check("hay keyframes periódicos", keys >= 1, "%d" % keys)
        starts = sum(1 for _, _, d in frames
                     if d[:4] == b"\x00\x00\x00\x01" or d[:3] == b"\x00\x00\x01")
        ok &= check("todas las unidades son Annex-B", starts == len(frames),
                    "%d/%d" % (starts, len(frames)))
        ok &= check("los PTS avanzan",
                    frames[-1][1] > frames[0][1],
                    "%d -> %d us" % (frames[0][1], frames[-1][1]))
        sizes = [len(d) for _, _, d in frames]
        ok &= check("tamaños razonables",
                    max(sizes) < 2_000_000 and min(sizes) > 0,
                    "min %d, max %d bytes" % (min(sizes), max(sizes)))

    print("\n3) Ajuste en caliente")
    before = len(frames)
    st.set_bitrate(1500)
    st.set_max_rate(15)
    st.force_keyframe()
    time.sleep(1.5)
    ok &= check("el pipeline sigue vivo tras reconfigurar",
                len(frames) > before, "+%d frames" % (len(frames) - before))
    ok &= check("bitrate aplicado", st.bitrate_kbps == 1500)
    st.stop()

    print("\n4) Cola de envío con descarte por GOP")
    overflowed = {"n": 0}

    class FakeSock:
        def sendall(self, _data):
            time.sleep(0.05)

    def on_overflow():
        overflowed["n"] += 1

    sender = _Sender(FakeSock(), on_overflow, maxlen=4)
    sender.start()
    for i in range(40):
        sender.put(protocol.pack_frame(0, i * 1000, b"x" * 1000))
    time.sleep(0.4)
    sender.stop()
    ok &= check("la cola desbordó y pidió keyframe", overflowed["n"] > 0,
                "%d veces" % overflowed["n"])

    print("\n5) Formato del paquete")
    payload = b"\x00\x00\x00\x01\x67abc"
    packed = protocol.pack_frame(protocol.FLAG_KEYFRAME, 123456789, payload)
    ok &= check("cabecera de 14 bytes",
                len(packed) == 14 + len(payload), "%d" % len(packed))
    cfg_bytes = protocol.pack_config(protocol.CODEC_HEVC, 1920, 1200, 60, 8000)
    ok &= check("CONFIG de 28 bytes", len(cfg_bytes) == 28, "%d" % len(cfg_bytes))
    ok &= check("CONFIG empieza con el magic", cfg_bytes[:4] == b"BSCR")

    with open("/tmp/wire-sample.bin", "wb") as fh:
        fh.write(cfg_bytes)
        fh.write(packed)
        fh.write(protocol.pack_stats(7250, 58.4))
    print("  (muestra escrita en /tmp/wire-sample.bin para el lector Java)")

    mainloop.stop()
    print("\n%s" % ("TODAS LAS PRUEBAS PASARON" if ok else "HAY FALLOS"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
