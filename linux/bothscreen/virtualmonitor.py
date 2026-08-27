"""Crea un monitor virtual en GNOME/Wayland usando la API privada de Mutter.

Mutter expone `org.gnome.Mutter.ScreenCast` en el bus de sesión. El método
`RecordVirtual` crea una región del escenario que no está respaldada por
hardware: para GNOME es un monitor más, aparece en Configuración -> Pantallas y
se puede arrastrar/posicionar como cualquier otro.

Desde GNOME 50 (el que trae Ubuntu 26.04) `RecordVirtual` acepta la propiedad
`modes`, con lo que podemos fijar exactamente 1920x1200@60 en vez de dejar que
PipeWire negocie un tamaño arbitrario. En versiones anteriores la clave se
ignora y el tamaño se negocia desde los caps de GStreamer, que también fijamos.

No se usa el portal xdg-desktop-portal a propósito: el portal abriría un diálogo
de selección de pantalla cada vez, y no permite pedir un monitor virtual con un
modo concreto.
"""

import logging
import threading

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

log = logging.getLogger(__name__)

SC_NAME = "org.gnome.Mutter.ScreenCast"
SC_PATH = "/org/gnome/Mutter/ScreenCast"
SC_IFACE = "org.gnome.Mutter.ScreenCast"
SESSION_IFACE = "org.gnome.Mutter.ScreenCast.Session"
STREAM_IFACE = "org.gnome.Mutter.ScreenCast.Stream"

CURSOR_MODE_HIDDEN = 0
CURSOR_MODE_EMBEDDED = 1
CURSOR_MODE_METADATA = 2


class VirtualMonitorError(RuntimeError):
    pass


class VirtualMonitor:
    """Sesión de screencast que publica un monitor virtual por PipeWire."""

    def __init__(self, width, height, refresh_rate=60.0,
                 cursor_mode=CURSOR_MODE_EMBEDDED, is_platform=True):
        self.width = int(width)
        self.height = int(height)
        self.refresh_rate = float(refresh_rate)
        self.cursor_mode = cursor_mode
        self.is_platform = is_platform

        self._bus = None
        self._session_path = None
        self._session = None
        self._stream = None
        self._node_id = None
        self._closed_cb = None

    # ------------------------------------------------------------------ util
    def _proxy(self, path, iface):
        return Gio.DBusProxy.new_sync(
            self._bus,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            None,
            SC_NAME,
            path,
            iface,
            None,
        )

    def _record_virtual_props(self, with_modes):
        props = {
            "cursor-mode": GLib.Variant("u", self.cursor_mode),
            "is-platform": GLib.Variant("b", self.is_platform),
        }
        if with_modes:
            mode = {
                "size": GLib.Variant("(uu)", (self.width, self.height)),
                "refresh-rate": GLib.Variant("d", self.refresh_rate),
                "is-preferred": GLib.Variant("b", True),
            }
            props["modes"] = GLib.Variant("aa{sv}", [mode])
        return GLib.Variant("(a{sv})", (props,))

    # ----------------------------------------------------------------- ciclo
    def start(self, timeout_ms=8000):
        """Levanta la sesión y devuelve el node-id de PipeWire del stream."""
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        try:
            sc = self._proxy(SC_PATH, SC_IFACE)
            version = sc.get_cached_property("Version")
            log.info("Mutter ScreenCast API versión %s",
                     version.unpack() if version else "desconocida")
            res = sc.call_sync(
                "CreateSession",
                GLib.Variant("(a{sv})", ({},)),
                Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as exc:
            raise VirtualMonitorError(
                "No se pudo hablar con org.gnome.Mutter.ScreenCast. "
                "¿Estás en una sesión GNOME (Wayland)? Detalle: %s" % exc.message
            ) from exc

        self._session_path = res.unpack()[0]
        self._session = self._proxy(self._session_path, SESSION_IFACE)
        log.debug("sesión de screencast: %s", self._session_path)

        stream_path = None
        last_error = None
        for with_modes in (True, False):
            try:
                out = self._session.call_sync(
                    "RecordVirtual",
                    self._record_virtual_props(with_modes),
                    Gio.DBusCallFlags.NONE, -1, None,
                )
                stream_path = out.unpack()[0]
                if not with_modes:
                    log.warning(
                        "Este Mutter no acepta 'modes'; el tamaño se negociará "
                        "desde GStreamer."
                    )
                break
            except GLib.Error as exc:
                last_error = exc
                log.debug("RecordVirtual(modes=%s) falló: %s", with_modes, exc.message)

        if stream_path is None:
            self.stop()
            raise VirtualMonitorError(
                "RecordVirtual falló: %s" % (last_error.message if last_error else "?")
            )

        self._stream = self._proxy(stream_path, STREAM_IFACE)

        # La señal PipeWireStreamAdded la despacha el bucle GLib que corre en su
        # propio hilo (ver mainloop.py), así que aquí solo esperamos el evento.
        state = {"node": None}
        arrived = threading.Event()

        def on_signal(_proxy, _sender, signal, params):
            if signal == "PipeWireStreamAdded":
                state["node"] = params.unpack()[0]
                arrived.set()

        handler = self._stream.connect("g-signal", on_signal)

        try:
            self._session.call_sync("Start", None, Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error as exc:
            self._stream.disconnect(handler)
            self.stop()
            raise VirtualMonitorError("Session.Start falló: %s" % exc.message) from exc

        arrived.wait(timeout_ms / 1000.0)
        self._stream.disconnect(handler)

        if state["node"] is None:
            self.stop()
            raise VirtualMonitorError(
                "no llegó PipeWireStreamAdded en %d ms; ¿hay un bucle GLib "
                "corriendo?" % timeout_ms)

        self._node_id = state["node"]
        log.info("Monitor virtual %dx%d@%.0f activo (nodo PipeWire %d)",
                 self.width, self.height, self.refresh_rate, self._node_id)
        return self._node_id

    @property
    def node_id(self):
        return self._node_id

    def stop(self):
        if self._session is not None:
            try:
                self._session.call_sync("Stop", None, Gio.DBusCallFlags.NONE,
                                        2000, None)
            except GLib.Error as exc:
                log.debug("Session.Stop: %s", exc.message)
        self._session = None
        self._stream = None
        self._node_id = None
