"""Reproduce el ciclo Iniciar -> Detener -> Iniciar de la ventana de control.

Este es el test que faltaba en la 1.0.0: `test_session.py` solo probaba que la
*tablet* se desconectara y volviera, nunca que el *servidor* se pudiera parar y
arrancar de nuevo, que es justo lo que hace el botón de la interfaz.
"""

import socket
import sys
import time

sys.path.insert(0, "..")

from bothscreen import encoder, mainloop, server  # noqa: E402
from test_pipeline import TestStreamer, check  # noqa: E402
from test_session import FakeMonitor, FakeTablet  # noqa: E402

PORT = 28997


def port_libre(port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
        return True, ""
    except OSError as exc:
        return False, str(exc)
    finally:
        probe.close()


def main():
    mainloop.start()
    server.VirtualMonitor = FakeMonitor
    encoder.Streamer = TestStreamer
    ok = True

    cfg = server.Config()
    cfg.port = PORT
    cfg.fps = 30

    print("\n1) Primer arranque")
    srv = server.DisplayServer(cfg)
    srv.start()
    tablet = FakeTablet(cfg.port)
    tablet.start()
    time.sleep(3.0)
    ok &= check("la tablet recibe vídeo", len(tablet.frames) > 10,
                "%d frames" % len(tablet.frames))
    session = srv.session

    print("\n2) Detener")
    srv.stop()
    time.sleep(1.0)
    ok &= check("la sesión quedó marcada como parada",
                session is not None and not session.running)
    ok &= check("el monitor virtual se retiró",
                FakeMonitor.instances[-1].stopped)

    antes = len(tablet.frames)
    time.sleep(1.5)
    ok &= check("la tablet DEJA de recibir vídeo",
                len(tablet.frames) == antes,
                "siguieron llegando %d frames" % (len(tablet.frames) - antes))
    ok &= check("el socket de la tablet se cerró", tablet.closed)

    # El bug de la 1.0.0: el hilo de accept seguía vivo y atendía conexiones
    # nuevas aunque la interfaz diera el servidor por parado, así que la tablet
    # volvía a recibir vídeo "sola".
    zombi = FakeTablet(cfg.port)
    zombi.start()
    time.sleep(2.0)
    ok &= check("una tablet que se reconecta NO es atendida",
                zombi.config is None and not zombi.frames,
                "recibió %d frames" % len(zombi.frames))
    ok &= check("ningún monitor virtual fantasma",
                len(FakeMonitor.instances) == 1,
                "%d monitores creados" % len(FakeMonitor.instances))
    zombi.stop()

    libre, detalle = port_libre(cfg.port)
    ok &= check("el puerto quedó libre", libre, detalle)

    print("\n3) Segundo arranque")
    srv2 = server.DisplayServer(cfg)
    try:
        srv2.start()
        arranco, detalle = True, ""
    except OSError as exc:
        arranco, detalle = False, str(exc)
    ok &= check("el servidor vuelve a arrancar", arranco, detalle)

    if arranco:
        tablet2 = FakeTablet(cfg.port)
        tablet2.start()
        time.sleep(3.0)
        ok &= check("la tablet vuelve a recibir vídeo",
                    len(tablet2.frames) > 10, "%d frames" % len(tablet2.frames))
        ok &= check("sin errores en la tablet", tablet2.error is None,
                    str(tablet2.error))
        tablet2.stop()
        srv2.stop()
        time.sleep(0.8)

    print("\n4) Tercer ciclo, para descartar acumulación")
    srv3 = server.DisplayServer(cfg)
    try:
        srv3.start()
        tablet3 = FakeTablet(cfg.port)
        tablet3.start()
        time.sleep(2.5)
        ok &= check("tercer arranque también funciona",
                    len(tablet3.frames) > 5, "%d frames" % len(tablet3.frames))
        tablet3.stop()
        srv3.stop()
    except OSError as exc:
        ok &= check("tercer arranque también funciona", False, str(exc))

    time.sleep(0.8)
    import threading
    vivos = [t.name for t in threading.enumerate()
             if t.name in ("sender", "ctl-reader", "adaptador", "accept")]
    ok &= check("no quedan hilos de trabajo vivos", not vivos, str(vivos))

    mainloop.stop()
    print("\n%s" % ("TODAS LAS PRUEBAS PASARON" if ok else "HAY FALLOS"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
