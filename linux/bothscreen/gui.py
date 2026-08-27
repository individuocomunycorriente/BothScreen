"""Ventana de control en GTK4/libadwaita.

En modo gráfico no se levanta el bucle GLib aparte: el propio bucle de GTK, que
corre en el hilo principal, ya despacha las señales D-Bus de Mutter y los
mensajes del bus de GStreamer. El servidor sigue viviendo en hilos normales y
todo lo que toca la interfaz pasa por GLib.idle_add.
"""

import logging
import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import __version__, adb, encoder, settings
from .server import DisplayServer

log = logging.getLogger(__name__)

AUTOR = "Creado por Danko Leiva"

RESOLUCIONES = [
    ("1920 × 1200  (recomendado, 16:10 nativo)", (1920, 1200)),
    ("1920 × 1080  (16:9, con bandas)", (1920, 1080)),
    ("1600 × 1000  (menos ancho de banda)", (1600, 1000)),
    ("2560 × 1600  (nativo, exige mucho más)", (2560, 1600)),
]

FPS_OPCIONES = [30, 60]

RUTAS_LOGO = [
    "/usr/share/icons/hicolor/256x256/apps/bothscreen.png",
    "/usr/share/bothscreen/logo.png",
]


def _indice(lista, valor, por_defecto=0):
    for i, elemento in enumerate(lista):
        if elemento == valor:
            return i
    return por_defecto


class Window(Adw.ApplicationWindow):

    def __init__(self, app, cfg, args):
        super().__init__(application=app, title="BothScreen")
        self.cfg = cfg
        self.args = args
        self.server = None
        self.serial = None
        self.set_default_size(480, 640)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        page = Adw.PreferencesPage()
        scroller.set_child(page)
        toolbar.set_content(scroller)
        self.set_content(toolbar)

        page.add(self._grupo_marca())
        page.add(self._grupo_estado())
        page.add(self._grupo_calidad())
        page.add(self._grupo_avanzado())
        page.add(self._grupo_botones())

        self.connect("close-request", self.on_close)

    # ------------------------------------------------------------ construcción
    def _grupo_marca(self):
        grupo = Adw.PreferencesGroup()
        caja = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                       margin_top=8, margin_bottom=8,
                       halign=Gtk.Align.CENTER)

        logo = None
        for ruta in RUTAS_LOGO:
            if os.path.isfile(ruta):
                logo = Gtk.Picture.new_for_filename(ruta)
                logo.set_size_request(88, 88)
                logo.set_content_fit(Gtk.ContentFit.CONTAIN)
                break
        if logo is None:
            logo = Gtk.Image.new_from_icon_name("video-display-symbolic")
            logo.set_pixel_size(72)
        caja.append(logo)

        titulo = Gtk.Label(label="BothScreen")
        titulo.add_css_class("title-1")
        caja.append(titulo)

        autor = Gtk.Label(label=AUTOR)
        autor.add_css_class("dim-label")
        caja.append(autor)

        version = Gtk.Label(label="versión %s" % __version__)
        version.add_css_class("dim-label")
        version.add_css_class("caption")
        caja.append(version)

        grupo.add(caja)
        return grupo

    def _grupo_estado(self):
        grupo = Adw.PreferencesGroup(title="Estado")
        self.status_row = Adw.ActionRow(
            title="Detenido",
            subtitle="Conecta la tablet por USB-C y pulsa Iniciar")
        grupo.add(self.status_row)

        self.stats_row = Adw.ActionRow(title="Caudal", subtitle="—")
        grupo.add(self.stats_row)

        self.pipeline_row = Adw.ActionRow(title="Codificación", subtitle="—")
        grupo.add(self.pipeline_row)
        return grupo

    def _grupo_calidad(self):
        grupo = Adw.PreferencesGroup(title="Calidad")

        self.res_row = Adw.ComboRow(title="Resolución")
        self.res_row.set_model(
            Gtk.StringList.new([r[0] for r in RESOLUCIONES]))
        self.res_row.set_selected(
            _indice([r[1] for r in RESOLUCIONES],
                    (self.cfg.max_width, self.cfg.max_height)))
        grupo.add(self.res_row)

        self.fps_row = Adw.ComboRow(title="Fotogramas por segundo")
        self.fps_row.set_model(Gtk.StringList.new([str(f) for f in FPS_OPCIONES]))
        self.fps_row.set_selected(_indice(FPS_OPCIONES, self.cfg.fps, 1))
        grupo.add(self.fps_row)

        self.codec_row = Adw.ComboRow(title="Códec")
        self.codec_row.set_model(Gtk.StringList.new(
            ["HEVC (menos ancho de banda)", "H.264 (máxima compatibilidad)"]))
        self.codec_row.set_selected(0 if self.cfg.prefer_hevc else 1)
        grupo.add(self.codec_row)

        self.adaptive_row = Adw.SwitchRow(
            title="Ajuste automático",
            subtitle="Sube y baja el bitrate según lo que aguante el enlace")
        self.adaptive_row.set_active(self.cfg.adaptive)
        grupo.add(self.adaptive_row)

        self.bitrate_row = Adw.SpinRow.new_with_range(2000, 30000, 500)
        self.bitrate_row.set_title("Bitrate máximo (kbps)")
        self.bitrate_row.set_value(self.cfg.max_bitrate)
        grupo.add(self.bitrate_row)
        return grupo

    def _grupo_avanzado(self):
        grupo = Adw.PreferencesGroup(title="Puntero del ratón")

        self.cursor_row = Adw.SwitchRow(
            title="Mostrar el puntero",
            subtitle="Incrusta el cursor en el vídeo. Obliga a capturar por "
                     "memoria en vez de DMABuf, que es el único camino en el "
                     "que GNOME dibuja el cursor en una pantalla virtual: "
                     "cuesta algo de CPU. Apágalo para máxima eficiencia.")
        self.cursor_row.set_active(self.cfg.cursor_mode != 0)
        grupo.add(self.cursor_row)

        self.platform_row = Adw.SwitchRow(
            title="Monitor de plataforma",
            subtitle="Cambia cómo declara GNOME la pantalla virtual. No "
                     "afecta al puntero; déjalo apagado salvo que tengas un "
                     "motivo. Para salir de dudas: "
                     "bothscreen --diagnostico")
        self.platform_row.set_active(self.cfg.is_platform)
        grupo.add(self.platform_row)
        return grupo

    def _grupo_botones(self):
        grupo = Adw.PreferencesGroup()
        caja = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                       margin_top=8)
        self.toggle = Gtk.Button(label="Iniciar")
        self.toggle.add_css_class("suggested-action")
        self.toggle.add_css_class("pill")
        self.toggle.set_hexpand(True)
        self.toggle.connect("clicked", self.on_toggle)
        caja.append(self.toggle)

        instalar = Gtk.Button(label="Reinstalar app")
        instalar.add_css_class("pill")
        instalar.connect("clicked", self.on_install)
        caja.append(instalar)
        grupo.add(caja)
        return grupo

    # -------------------------------------------------------------- acciones
    def collect_config(self):
        self.cfg.max_width, self.cfg.max_height = \
            RESOLUCIONES[self.res_row.get_selected()][1]
        self.cfg.fps = FPS_OPCIONES[self.fps_row.get_selected()]
        self.cfg.prefer_hevc = self.codec_row.get_selected() == 0
        self.cfg.adaptive = self.adaptive_row.get_active()
        self.cfg.cursor_mode = 1 if self.cursor_row.get_active() else 0
        self.cfg.is_platform = self.platform_row.get_active()
        self.cfg.max_bitrate = int(self.bitrate_row.get_value())
        self.cfg.start_bitrate = min(self.cfg.start_bitrate, self.cfg.max_bitrate)
        settings.save(self.cfg)

    def on_toggle(self, _button):
        if self.server is None:
            self.collect_config()
            self.start_server()
        else:
            self.stop_server()

    def _sensibilidad_ajustes(self, activos):
        for fila in (self.res_row, self.fps_row, self.codec_row,
                     self.adaptive_row, self.cursor_row, self.platform_row,
                     self.bitrate_row):
            fila.set_sensitive(activos)

    def start_server(self):
        self.toggle.set_sensitive(False)
        self.status_row.set_title("Preparando…")

        def work():
            server = None
            try:
                from .app import prepare_device
                serial = None
                if not self.args.no_adb:
                    serial = prepare_device(self.cfg)
                server = DisplayServer(self.cfg, on_state=self.on_state)
                server.start()
                if not self.args.no_adb:
                    adb.launch_app(self.cfg.port, serial)
                GLib.idle_add(self._started, server, serial)
            except SystemExit as exc:
                self._deshacer(server)
                GLib.idle_add(self._failed, str(exc))
            except Exception as exc:
                log.exception("no se pudo iniciar")
                self._deshacer(server)
                GLib.idle_add(self._failed, str(exc))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _deshacer(server):
        """Si el arranque falla a medias, no dejar nada a medio abrir."""
        if server is not None:
            try:
                server.stop()
            except Exception:
                log.exception("fallo al deshacer el arranque")

    def _started(self, server, serial):
        self.server = server
        self.serial = serial
        self.toggle.set_label("Detener")
        self.toggle.remove_css_class("suggested-action")
        self.toggle.add_css_class("destructive-action")
        self.toggle.set_sensitive(True)
        self._sensibilidad_ajustes(False)
        self.status_row.set_title("Esperando a la tablet")
        self.status_row.set_subtitle("La app debería abrirse sola en la Tab S7")
        return False

    def _failed(self, message):
        self.server = None
        self.serial = None
        self.toggle.set_sensitive(True)
        self._sensibilidad_ajustes(True)
        self.status_row.set_title("No se pudo iniciar")
        self.status_row.set_subtitle(message)
        return False

    def stop_server(self):
        self.toggle.set_sensitive(False)
        if self.server:
            self.server.stop()
            self.server = None
        if self.serial and not self.args.no_adb:
            adb.remove_reverse(self.cfg.port, self.serial)
            adb.stop_app(self.serial)
        self.serial = None
        self.toggle.set_label("Iniciar")
        self.toggle.remove_css_class("destructive-action")
        self.toggle.add_css_class("suggested-action")
        self.toggle.set_sensitive(True)
        self._sensibilidad_ajustes(True)
        self.status_row.set_title("Detenido")
        self.status_row.set_subtitle(
            "Conecta la tablet por USB-C y pulsa Iniciar")
        self.stats_row.set_subtitle("—")
        self.pipeline_row.set_subtitle("—")

    def on_install(self, _button):
        def work():
            try:
                from .app import find_apk
                apk = find_apk()
                serial = adb.usb_device()
                if apk is None:
                    raise RuntimeError("no encuentro el APK empaquetado")
                if serial is None:
                    raise RuntimeError("no veo la tablet por USB")
                adb.install_apk(apk, serial)
                GLib.idle_add(self._toast, "App instalada en la tablet")
            except Exception as exc:
                GLib.idle_add(self._toast, "Error: %s" % exc)

        threading.Thread(target=work, daemon=True).start()

    def _toast(self, text):
        self.status_row.set_subtitle(text)
        return False

    # ---------------------------------------------------------------- estado
    def on_state(self, state):
        GLib.idle_add(self._apply_state, state)

    def _apply_state(self, state):
        if self.server is None:
            return False
        if not state.get("running"):
            self.status_row.set_title("Esperando a la tablet")
            self.stats_row.set_subtitle("—")
            return False
        self.status_row.set_title("Transmitiendo")
        self.status_row.set_subtitle("%dx%d @ %d fps · %s" % (
            state["width"], state["height"], state["fps"], state["codec"]))
        self.stats_row.set_subtitle(
            "%.1f Mbps  ·  %.0f fps reales  ·  %d frames en vuelo" % (
                state["bitrate_actual"] / 1000.0, state["fps_actual"],
                state["in_flight"]))
        self.pipeline_row.set_subtitle(
            "%s (%s), objetivo %d kbps" % (
                "hardware VA-API" if state["hardware"] else "software",
                state["pipeline"], state["bitrate_target"]))
        return False

    def on_close(self, *_args):
        self.stop_server()
        return False


class Application(Adw.Application):

    def __init__(self, cfg, args):
        super().__init__(application_id="cl.danko.BothScreen")
        self.cfg = cfg
        self.args = args
        self.window = None

    def do_activate(self):
        encoder.init_gst()
        if self.window is None:
            self.window = Window(self, self.cfg, self.args)
        self.window.present()

    def do_shutdown(self):
        """Último recurso: que no quede nada abierto pase lo que pase."""
        if self.window is not None:
            try:
                self.window.stop_server()
            except Exception:
                log.exception("fallo al cerrar el servidor")
        if not self.args.no_adb:
            adb.shutdown()
        Adw.Application.do_shutdown(self)


def run_gui(cfg, args):
    return Application(cfg, args).run([])
