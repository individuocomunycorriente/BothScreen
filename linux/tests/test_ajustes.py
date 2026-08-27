"""Ajustes persistentes, precedencia de opciones y limpieza del servidor adb."""

import os
import socket
import sys
import tempfile

sys.path.insert(0, "..")

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="sp-cfg-")

from bothscreen import adb, app, settings  # noqa: E402
from bothscreen.server import Config  # noqa: E402
from test_pipeline import check  # noqa: E402


def main():
    ok = True

    print("\n1) Guardar y recuperar ajustes")
    cfg = Config()
    cfg.max_width, cfg.max_height = 1600, 1000
    cfg.fps = 30
    cfg.is_platform = True
    cfg.cursor_mode = 0
    cfg.prefer_hevc = False
    settings.save(cfg)

    otro = settings.load(Config())
    ok &= check("resolución", (otro.max_width, otro.max_height) == (1600, 1000))
    ok &= check("fps", otro.fps == 30)
    ok &= check("monitor de plataforma", otro.is_platform is True)
    ok &= check("modo de cursor", otro.cursor_mode == 0)
    ok &= check("códec", otro.prefer_hevc is False)

    print("\n2) Un archivo corrupto no rompe nada")
    with open(settings.config_path(), "w") as fh:
        fh.write("{esto no es json")
    limpio = settings.load(Config())
    ok &= check("se cae a los valores por defecto",
                (limpio.max_width, limpio.fps) == (1920, 60))

    print("\n3) Claves con el tipo equivocado se ignoran")
    with open(settings.config_path(), "w") as fh:
        fh.write('{"fps": "sesenta", "max_width": 1600, "inventada": 1}')
    mixto = settings.load(Config())
    ok &= check("el fps inválido se descarta", mixto.fps == 60, str(mixto.fps))
    ok &= check("la clave válida sí se aplica", mixto.max_width == 1600)
    ok &= check("las claves desconocidas no se cuelan",
                not hasattr(mixto, "inventada"))

    print("\n4) Precedencia: por defecto < guardado < línea de órdenes")
    guardado = Config()
    guardado.fps = 30
    guardado.max_width, guardado.max_height = 1600, 1000
    guardado.is_platform = True
    settings.save(guardado)

    parser = app.build_parser()
    cfg_a = app.config_from_args(parser.parse_args([]), parser)
    ok &= check("sin argumentos manda lo guardado",
                cfg_a.fps == 30 and cfg_a.max_width == 1600
                and cfg_a.is_platform is True)

    cfg_b = app.config_from_args(
        parser.parse_args(["--fps", "60", "--no-platform-monitor"]), parser)
    ok &= check("la línea de órdenes pisa lo guardado",
                cfg_b.fps == 60 and cfg_b.is_platform is False)
    ok &= check("lo no especificado sigue viniendo del archivo",
                cfg_b.max_width == 1600)

    cfg_c = app.config_from_args(
        parser.parse_args(["--sin-ajustes-guardados"]), parser)
    ok &= check("se puede ignorar el archivo por completo",
                cfg_c.fps == 60 and cfg_c.max_width == 1920
                and cfg_c.is_platform is False)

    print("\n5) El servidor adb solo se apaga si lo levantamos nosotros")
    ajeno = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ajeno.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        ajeno.bind(("127.0.0.1", adb.ADB_SERVER_PORT))
        ajeno.listen(1)
        adb._we_started_the_server = None
        adb._note_server_ownership()
        ok &= check("se detecta un adb ajeno ya en marcha",
                    adb._we_started_the_server is False)
        llamadas = []
        original = adb.subprocess.run
        adb.subprocess.run = lambda *a, **k: llamadas.append(a)
        adb.shutdown()
        adb.subprocess.run = original
        ok &= check("no se le mata el adb al usuario", not llamadas)
    except OSError as exc:
        ok &= check("se pudo simular un adb ajeno", False, str(exc))
    finally:
        ajeno.close()

    adb._we_started_the_server = None
    adb._note_server_ownership()
    ok &= check("sin adb previo, nos declaramos dueños",
                adb._we_started_the_server is True)

    print("\n%s" % ("TODAS LAS PRUEBAS PASARON" if ok else "HAY FALLOS"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
