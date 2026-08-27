"""Punto de entrada: orquesta adb, el servidor y (opcionalmente) la interfaz."""

import argparse
import logging
import os
import signal
import sys
import time

from . import adb, encoder, mainloop, protocol, settings
from .server import Config, DisplayServer

log = logging.getLogger("bothscreen")

APK_PATHS = [
    "/usr/share/bothscreen/bothscreen.apk",
    os.path.join(os.path.dirname(__file__), "..", "bothscreen.apk"),
]


def find_apk():
    for path in APK_PATHS:
        path = os.path.abspath(path)
        if os.path.isfile(path):
            return path
    return None


def build_parser():
    p = argparse.ArgumentParser(
        prog="bothscreen",
        description="Usa una tablet Android como segunda pantalla por USB-C.")
    # Las opciones que la ventana recuerda llevan default=None a propósito: así
    # se distingue «no lo has dicho» de «lo has dicho y coincide con el valor
    # de fábrica», que es justo lo que hace falta para que la línea de órdenes
    # pise siempre al archivo de ajustes.
    p.add_argument("--port", type=int, default=None,
                   help="puerto del túnel (por defecto 27183)")
    p.add_argument("--size", default=None,
                   help="resolución máxima de la pantalla virtual "
                        "(por defecto 1920x1200)")
    p.add_argument("--fps", type=int, default=None,
                   help="fps máximos (por defecto 60)")
    p.add_argument("--bitrate", type=int, default=6000,
                   help="bitrate inicial en kbps")
    p.add_argument("--max-bitrate", type=int, default=None,
                   help="techo del bitrate en kbps (por defecto 14000)")
    p.add_argument("--min-bitrate", type=int, default=1500)
    p.add_argument("--codec", choices=("auto", "h264", "hevc"), default=None,
                   help="auto prefiere HEVC, que gasta menos ancho de banda")
    p.add_argument("--rate-control", choices=("vbr", "cbr", "cqp"), default="vbr")
    p.add_argument("--adaptive", dest="adaptive", action="store_true",
                   default=None, help="ajusta el bitrate solo (por defecto)")
    p.add_argument("--no-adaptive", dest="adaptive", action="store_false",
                   help="fija el bitrate en vez de ajustarlo solo")
    p.add_argument("--software", action="store_true",
                   help="fuerza codificación por software (diagnóstico)")
    p.add_argument("--no-cursor", action="store_true",
                   help="no dibujar el puntero del ratón en la tablet")
    p.add_argument("--platform-monitor", dest="platform_monitor",
                   action="store_true", default=None,
                   help="declara la pantalla virtual como monitor de "
                        "plataforma; pruébalo si no se ve el puntero")
    p.add_argument("--no-platform-monitor", dest="platform_monitor",
                   action="store_false",
                   help="valor por defecto: pantalla virtual normal")
    p.add_argument("--no-adb", action="store_true",
                   help="no tocar adb; útil si ya montaste el túnel a mano")
    p.add_argument("--install", action="store_true",
                   help="instala/actualiza el APK en la tablet y sale")
    p.add_argument("--diagnostico", action="store_true",
                   help="averigua qué ajuste hace visible el puntero")
    p.add_argument("--sin-ajustes-guardados", dest="ignore_saved",
                   action="store_true",
                   help="ignora ~/.config/bothscreen/config.json")
    p.add_argument("--gui", action="store_true", help="abre la ventana de control")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def parse_size(text):
    try:
        w, h = text.lower().split("x")
        return int(w), int(h)
    except Exception:
        raise argparse.ArgumentTypeError("tamaño inválido: %s" % text)


def config_from_args(args, parser=None):
    """Precedencia: valores de fábrica < ajustes guardados < línea de órdenes.

    Las opciones recordables valen None cuando el usuario no las escribió, así
    que basta con comprobar si llegan puestas.
    """
    cfg = Config()
    if not args.ignore_saved:
        settings.load(cfg)

    if args.port is not None:
        cfg.port = args.port
    if args.size is not None:
        cfg.max_width, cfg.max_height = parse_size(args.size)
    if args.fps is not None:
        cfg.fps = args.fps
    if args.max_bitrate is not None:
        cfg.max_bitrate = args.max_bitrate
    if args.codec is not None:
        cfg.prefer_hevc = args.codec != "h264"
    if args.adaptive is not None:
        cfg.adaptive = args.adaptive
    if args.platform_monitor is not None:
        cfg.is_platform = args.platform_monitor
    if args.no_cursor:
        cfg.cursor_mode = 0

    cfg.start_bitrate = min(args.bitrate, cfg.max_bitrate)
    cfg.min_bitrate = args.min_bitrate
    cfg.prefer_hardware = not args.software
    cfg.rate_control = args.rate_control
    return cfg


def check_session():
    """Avisa temprano si esto no es una sesión GNOME Wayland."""
    session_type = os.environ.get("XDG_SESSION_TYPE", "")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if "GNOME" not in desktop.upper():
        log.warning("El escritorio actual es '%s'. Esta herramienta usa la API "
                    "de monitores virtuales de Mutter, que solo existe en "
                    "GNOME.", desktop or "desconocido")
    if session_type and session_type != "wayland":
        log.warning("Sesión '%s': en X11 GNOME no expone monitores virtuales "
                    "por esta API. Inicia sesión en «GNOME» (Wayland).",
                    session_type)


def prepare_device(cfg, install=False):
    """Deja el túnel USB montado y la app abierta en la tablet."""
    if adb.adb_path() is None:
        raise SystemExit("Falta adb. Instálalo con: sudo apt install adb")

    serial = adb.usb_device()
    if serial is None:
        pending = adb.devices()
        if any(state == "unauthorized" for _, state in pending):
            raise SystemExit(
                "La tablet aparece como 'unauthorized': acepta el diálogo de "
                "depuración USB en la pantalla de la tablet y reintenta.")
        raise SystemExit(
            "No veo ninguna tablet por USB. Revisa que el cable esté conectado "
            "y que la depuración USB esté activada en la Tab S7.")

    log.info("tablet: %s", serial)
    adb.remove_legacy_apps(serial)

    apk = find_apk()
    if install or not adb.is_app_installed(serial):
        if apk is None:
            raise SystemExit(
                "No encuentro el APK. Instálalo a mano con: adb install -r "
                "bothscreen.apk")
        log.info("instalando la app en la tablet…")
        adb.install_apk(apk, serial)

    adb.reverse(cfg.port, serial)
    return serial


def run_headless(cfg, args):
    mainloop.start()
    encoder.init_gst()

    serial = None
    if not args.no_adb:
        serial = prepare_device(cfg)

    server = DisplayServer(cfg)
    server.start()

    if not args.no_adb:
        time.sleep(0.3)
        adb.launch_app(cfg.port, serial)

    codecs = encoder.available_codecs()
    log.info("códecs disponibles en el PC: %s%s",
             "H.264 " if codecs & protocol.CODEC_BIT_H264 else "",
             "HEVC" if codecs & protocol.CODEC_BIT_HEVC else "")
    log.info("Listo. La pantalla virtual aparece cuando la tablet conecta. "
             "Ctrl+C para salir.")

    stopping = {"flag": False}

    def handle_signal(_signum, _frame):
        stopping["flag"] = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while not stopping["flag"]:
            time.sleep(0.25)
    finally:
        log.info("cerrando…")
        server.stop()
        if not args.no_adb and serial:
            adb.remove_reverse(cfg.port, serial)
            adb.stop_app(serial)
        mainloop.stop()
        if not args.no_adb:
            adb.shutdown()
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S")

    cfg = config_from_args(args, parser)
    check_session()

    if args.diagnostico:
        from . import diagnose
        mainloop.start()
        try:
            return diagnose.run()
        finally:
            mainloop.stop()

    if args.install:
        apk = find_apk()
        if apk is None:
            raise SystemExit("No encuentro el APK que instalar.")
        serial = adb.usb_device()
        if serial is None:
            raise SystemExit("No veo la tablet por USB.")
        print(adb.install_apk(apk, serial))
        return 0

    if args.gui:
        from .gui import run_gui
        return run_gui(cfg, args)
    return run_headless(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
