"""Captura desde PipeWire y codificación por hardware (VA-API) con GStreamer.

Dos detalles que hacen que esto sea barato en ancho de banda:

1. El stream de Mutter está guiado por daño: si nada cambia en la pantalla
   virtual, no llegan buffers y no se transmite absolutamente nada. No hay que
   inventar detección de "pantalla estática", el compositor ya la hace.

2. El codificador trabaja en CBR con un GOP largo y sin B-frames. El bitrate y
   el tope de fps se ajustan en caliente desde el controlador adaptativo de
   `server.py` según cuántos frames lleva la tablet sin confirmar.

La Radeon 660M del Ryzen 7535HS (VCN 3.x) codifica H.264 y HEVC en hardware, así
que la CPU queda prácticamente libre. Si por lo que sea no hay VA-API, se cae a
x264enc en modo zerolatency.
"""

import logging
import re

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo, GLib  # noqa: E402

from . import protocol

log = logging.getLogger(__name__)

_initialized = False


def init_gst():
    global _initialized
    if not _initialized:
        Gst.init(None)
        _initialized = True


def _find_factory(*names):
    for name in names:
        if Gst.ElementFactory.find(name):
            return name
    return None


def _find_va_encoder(codec):
    """Busca vah264enc / vah265enc, o su variante con nombre de dispositivo."""
    suffix = "h264enc" if codec == protocol.CODEC_H264 else "h265enc"
    direct = _find_factory("va" + suffix)
    if direct:
        return direct
    pattern = re.compile(r"^va.*%s$" % suffix)
    registry = Gst.Registry.get()
    for feature in registry.get_feature_list(Gst.ElementFactory):
        if pattern.match(feature.get_name()):
            return feature.get_name()
    return None


def available_codecs():
    """Máscara de códecs que este PC puede codificar (hardware o software)."""
    init_gst()
    mask = 0
    if _find_va_encoder(protocol.CODEC_H264) or _find_factory("x264enc"):
        mask |= protocol.CODEC_BIT_H264
    if _find_va_encoder(protocol.CODEC_HEVC) or _find_factory("x265enc"):
        mask |= protocol.CODEC_BIT_HEVC
    return mask


def _nal_units(data):
    """Itera (inicio, fin) de cada NAL de un buffer Annex-B."""
    n = len(data)
    i = 0
    starts = []
    while i < n - 3:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                starts.append((i, i + 3))
                i += 3
                continue
            if data[i + 2] == 0 and i + 3 < n and data[i + 3] == 1:
                starts.append((i, i + 4))
                i += 4
                continue
        i += 1
    for idx, (sc_start, payload_start) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else n
        yield sc_start, payload_start, end


def split_parameter_sets(data, codec):
    """Separa VPS/SPS/PPS del resto de la unidad de acceso.

    MediaCodec espera la CSD en un buffer propio marcado con
    BUFFER_FLAG_CODEC_CONFIG. h264parse/h265parse, en cambio, entregan los
    parameter sets pegados delante del IDR (config-interval=-1). Si se envía
    todo junto marcado como configuración, el decodificador se traga el IDR y la
    imagen no aparece hasta el siguiente keyframe. Por eso se parte aquí.

    Devuelve (config_bytes o None, resto_bytes).
    """
    if codec == protocol.CODEC_HEVC:
        param_types = (32, 33, 34)  # VPS, SPS, PPS

        def nal_type(byte):
            return (byte >> 1) & 0x3F
    else:
        param_types = (7, 8)        # SPS, PPS

        def nal_type(byte):
            return byte & 0x1F

    # Se reparten los NAL en dos grupos conservando el orden. No basta con
    # cortar por el primero que no sea parameter set: h264parse suele anteponer
    # un AUD (tipo 9) y a veces un SEI, y quedarían delante de la CSD.
    config = bytearray()
    rest = bytearray()
    found = False
    for sc_start, payload_start, end in _nal_units(data):
        if payload_start >= len(data):
            continue
        chunk = data[sc_start:end]
        if nal_type(data[payload_start]) in param_types:
            config += chunk
            found = True
        else:
            rest += chunk

    if not found or not rest:
        return None, data
    return bytes(config), bytes(rest)


def _set_if_present(element, prop, value):
    """Los nombres de propiedades cambian entre versiones de GStreamer."""
    try:
        if element.find_property(prop) is not None:
            element.set_property(prop, value)
            return True
    except Exception:  # pragma: no cover - defensivo
        pass
    return False


class _RateLimiter:
    """Tope de fps mediante una sonda en un pad, sin retener nada.

    Sustituye a `videorate`. Ese elemento vive en la negociación de caps, y
    delante de una fuente guiada por daño estorba dos veces: mete su propio
    rango de framerate en la negociación (con lo que Mutter puede acabar
    creyendo que le basta con grabar muy de vez en cuando) y añade un elemento
    más que puede quedarse con un fotograma aislado. Una sonda decide al vuelo:
    o pasa o se descarta, y nunca guarda nada para después.
    """

    def __init__(self, fps):
        self.min_interval = 0
        self.set_fps(fps)
        self._last_pts = None
        self._probe_id = None
        self._pad = None

    def set_fps(self, fps):
        fps = max(int(fps), 1)
        self.min_interval = Gst.SECOND // fps

    def attach(self, pad):
        self._pad = pad
        self._probe_id = pad.add_probe(Gst.PadProbeType.BUFFER, self._on_buffer)

    def forget(self):
        """Suelta las referencias sin tocar el pad.

        Se llama cuando el pipeline entero se va a NULL: las sondas mueren con
        él, y llamar a remove_probe sobre un pad ya desmontado solo consigue un
        aviso de GStreamer.
        """
        self._pad = None
        self._probe_id = None
        self._last_pts = None

    def should_pass(self, pts):
        """Decide en el acto. Nunca guarda un fotograma para más tarde.

        Es la diferencia que importa frente a videorate: una actualización
        aislada (el puntero cruzando un escritorio quieto) sale ya, no espera a
        que llegue otra detrás.
        """
        if pts == Gst.CLOCK_TIME_NONE or pts is None:
            return True
        last = self._last_pts
        if last is not None:
            delta = pts - last
            # Si el reloj retrocede (reinicio del pipeline), se reengancha.
            if 0 <= delta < self.min_interval:
                return False
        self._last_pts = pts
        return True

    def _on_buffer(self, _pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        if self.should_pass(buf.pts):
            return Gst.PadProbeReturn.OK
        return Gst.PadProbeReturn.DROP


class Streamer:
    """Pipeline PipeWire -> VA-API -> appsink."""

    def __init__(self, node_id, width, height, fps, codec, bitrate_kbps,
                 on_frame, on_error=None, prefer_hardware=True,
                 rate_control="vbr", want_cursor=True):
        init_gst()
        self.rate_control = rate_control
        # Con puntero NO se puede usar el camino DMABuf. Mutter tiene dos
        # formas de entregar el fotograma y elige según lo que negocie el
        # cliente: si los caps llevan la característica memory:DMABuf,
        # pipewiresrc añade la propiedad `modifier` al formato, Mutter ve esa
        # propiedad y sirve buffers DMABuf. Y para un stream *virtual* el
        # camino DMABuf (`record_to_framebuffer`) es un `cogl_blit_framebuffer`
        # pelado, sin ningún tratamiento del cursor — a diferencia de los
        # streams de monitor y de área, que repintan con FORCE_CURSORS en los
        # dos caminos. Pidiendo `video/x-raw` a secas no hay modifier, Mutter
        # sirve MemFd y toma `record_to_buffer`, que sí repinta la escena con
        # CLUTTER_PAINT_FLAG_FORCE_CURSORS. Ese es el único camino en el que el
        # puntero llega a la tablet.
        self.want_cursor = want_cursor
        self.node_id = node_id
        self.width = width
        self.height = height
        self.fps = fps
        self.codec = codec
        self.bitrate_kbps = bitrate_kbps
        self.on_frame = on_frame
        self.on_error = on_error
        self.prefer_hardware = prefer_hardware

        self.pipeline = None
        self.encoder = None
        self.limiter = _RateLimiter(fps)
        self.appsink = None
        self.description = ""
        self.hardware = False
        self._sent_config = False
        self._starting = False
        self._bus_watched = False

    # ------------------------------------------------------------ pipelines
    def _candidates(self):
        codec_name = "h264" if self.codec == protocol.CODEC_H264 else "h265"
        parse = "h264parse" if self.codec == protocol.CODEC_H264 else "h265parse"
        caps_out = ("video/x-h264" if self.codec == protocol.CODEC_H264
                    else "video/x-h265")

        # always-copy=true no es un capricho. Mutter dibuja en un juego pequeño
        # de buffers de PipeWire y, cuando va a grabar un fotograma, si no tiene
        # ninguno libre simplemente NO lo graba y no reprograma nada: ese
        # fotograma se pierde para siempre. Con always-copy=false el buffer
        # viaja por todo el pipeline (cola, conversor, codificador) antes de
        # devolverse, así que basta un instante de retención para perder
        # actualizaciones sueltas — justo las que produce un puntero moviéndose
        # sobre un escritorio quieto. Copiando de entrada, el buffer vuelve a
        # Mutter enseguida y nunca se queda sin.
        src = (
            "pipewiresrc name=src path={node} do-timestamp=true "
            "keepalive-time=1000 resend-last=true always-copy=true"
        ).format(node=self.node_id)

        # Aquí NO se pide framerate. Mutter anuncia el suyo (normalmente 0/1,
        # variable, porque el stream va guiado por daño) y exigirle un valor
        # concreto rompe la negociación entera: pipewiresrc se queda "sin más
        # formatos de entrada" y ningún pipeline arranca. La cadencia de captura
        # ya se controla donde toca, en el refresh-rate del monitor virtual que
        # se pide por D-Bus, y el tope de fps lo aplica _RateLimiter aguas
        # abajo. Lo único que hace falta de este capsfilter es que NO lleve la
        # característica memory:DMABuf, para que el puntero se vea.
        rate_caps = "video/x-raw"
        queue = ("queue name=q max-size-buffers=5 max-size-time=0 "
                 "max-size-bytes=0 leaky=downstream")
        tail = (
            "{parse} name=parse config-interval=-1 ! "
            "{caps_out},stream-format=byte-stream,alignment=au ! "
            "appsink name=sink emit-signals=true sync=false max-buffers=4 drop=false"
        ).format(parse=parse, caps_out=caps_out)

        va_enc = _find_va_encoder(self.codec)
        out = []

        if va_enc and self.prefer_hardware:
            if not self.want_cursor:
                # Camino cero-copia: DMABuf de Mutter -> VAMemory -> codificador.
                # Solo sirve sin puntero (ver la nota larga más abajo).
                out.append((
                    "va-dmabuf", True, va_enc,
                    "{src} ! {queue} ! vapostproc name=vpp ! "
                    "video/x-raw(memory:VAMemory),format=NV12,"
                    "width={w},height={h} ! "
                    "{enc} name=venc ! {tail}".format(
                        src=src, queue=queue, enc=va_enc,
                        w=self.width, h=self.height, tail=tail)
                ))

            # Memoria de sistema, con la conversión y el escalado aún en la GPU.
            out.append((
                "va-memoria", True, va_enc,
                "{src} ! {rate_caps} ! {queue} ! vapostproc name=vpp ! "
                "video/x-raw(memory:VAMemory),format=NV12,width={w},height={h} ! "
                "{enc} name=venc ! {tail}".format(
                    src=src, queue=queue, rate_caps=rate_caps, enc=va_enc,
                    w=self.width, h=self.height, tail=tail)
            ))

            # Lo mismo pero convirtiendo por CPU, por si vapostproc no acepta
            # la entrada en memoria de sistema con este driver.
            out.append((
                "va-memoria-cpu", True, va_enc,
                "{src} ! {rate_caps} ! {queue} ! "
                "videoconvertscale name=vpp ! "
                "video/x-raw,format=NV12,width={w},height={h} ! "
                "{enc} name=venc ! {tail}".format(
                    src=src, queue=queue, rate_caps=rate_caps, enc=va_enc,
                    w=self.width, h=self.height, tail=tail)
            ))

        sw_enc = _find_factory(
            "x264enc" if self.codec == protocol.CODEC_H264 else "x265enc")
        if sw_enc:
            out.append((
                "software", False, sw_enc,
                "{src} ! {rate_caps} ! {queue} ! "
                "videoconvertscale name=vpp ! "
                "video/x-raw,format=I420,width={w},height={h} ! "
                "{enc} name=venc ! {tail}".format(
                    src=src, queue=queue, rate_caps=rate_caps, enc=sw_enc,
                    w=self.width, h=self.height, tail=tail)
            ))

        if not out:
            raise RuntimeError(
                "No hay ningún codificador %s disponible. Instala "
                "gstreamer1.0-plugins-bad (VA-API) o gstreamer1.0-plugins-ugly."
                % codec_name)
        return out

    def _tune_encoder(self, enc, factory_name):
        """Ajustes de baja latencia, tolerantes a nombres de propiedad."""
        gop = max(self.fps * 5, 30)
        _set_if_present(enc, "bitrate", self.bitrate_kbps)
        _set_if_present(enc, "b-frames", 0)
        _set_if_present(enc, "bframes", 0)
        _set_if_present(enc, "key-int-max", gop)
        _set_if_present(enc, "ref-frames", 1)
        _set_if_present(enc, "num-ref-frames", 1)

        if factory_name.startswith("va"):
            # VBR por defecto: el escritorio es muy a ráfagas (todo quieto, y de
            # golpe un scroll), así que un caudal variable con techo gasta mucho
            # menos que CBR sin perder calidad en los momentos que importan.
            try:
                enc.set_property("rate-control", self.rate_control)
            except Exception:
                _set_if_present(enc, "rate-control", 2)
            # 1 = máxima calidad, 7 = máxima velocidad. 4 mantiene la latencia
            # de codificación por debajo de un frame sin regalar calidad.
            _set_if_present(enc, "target-usage", 4)
            _set_if_present(enc, "mbbrc", True)
            _set_if_present(enc, "cpb-size", max(self.bitrate_kbps // 2, 500))
        elif factory_name == "x264enc":
            _set_if_present(enc, "tune", 0x00000004)  # zerolatency
            _set_if_present(enc, "speed-preset", 2)   # superfast
            _set_if_present(enc, "pass", 0)           # cbr
            _set_if_present(enc, "sliced-threads", True)
            _set_if_present(enc, "byte-stream", True)
        elif factory_name == "x265enc":
            _set_if_present(enc, "tune", 4)           # zerolatency
            _set_if_present(enc, "speed-preset", 3)

    def start(self):
        last_error = None
        # Mientras se buscan candidatos, los errores del bus son información,
        # no una avería: el plan es justo ese, que unos fallen y otro arranque.
        # Si se dejaran llegar a on_error, el primer candidato fallido cerraría
        # la sesión entera y los siguientes se quedarían sin monitor virtual.
        self._starting = True
        try:
            for name, hardware, factory, desc in self._candidates():
                log.info("probando pipeline '%s' (%s)", name, factory)
                try:
                    self._build(desc)
                except GLib.Error as exc:
                    last_error = exc
                    log.warning("no se pudo construir '%s': %s", name, exc)
                    continue

                self._tune_encoder(self.encoder, factory)
                err = self._try_play()
                if err is None:
                    self.description = name
                    self.hardware = hardware
                    self._starting = False
                    log.info("pipeline activo: %s (%s, %dx%d@%d, %d kbps)",
                             name, factory, self.width, self.height, self.fps,
                             self.bitrate_kbps)
                    return
                last_error = err
                log.warning("pipeline '%s' no arrancó: %s", name, err)
                self.stop()
        finally:
            self._starting = False

        raise RuntimeError("Ningún pipeline de captura funcionó: %s" % last_error)

    def _build(self, description):
        self.pipeline = Gst.parse_launch(description)
        self.encoder = self.pipeline.get_by_name("venc")
        self.appsink = self.pipeline.get_by_name("sink")

        # El tope de fps se aplica a la salida de la cola, antes de convertir y
        # codificar: así un fotograma descartado no cuesta ni GPU ni CPU.
        cola = self.pipeline.get_by_name("q")
        if cola is not None:
            self.limiter.attach(cola.get_static_pad("src"))
        self.appsink.connect("new-sample", self._on_sample)
        self._sent_config = False

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_watched = True
        bus.connect("message::error", self._on_bus_error)

    def _try_play(self):
        """Pasa a PLAYING y espera confirmación; devuelve None si todo bien."""
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            return "set_state(PLAYING) devolvió FAILURE"
        ret, _state, _pending = self.pipeline.get_state(5 * Gst.SECOND)
        if ret != Gst.StateChangeReturn.SUCCESS:
            bus = self.pipeline.get_bus()
            msg = bus.poll(Gst.MessageType.ERROR, 0)
            if msg:
                err, _dbg = msg.parse_error()
                return err.message
            return "no alcanzó PLAYING (%s)" % ret.value_nick
        return None

    def _on_bus_error(self, _bus, message):
        err, debug = message.parse_error()
        if self._starting:
            log.debug("GStreamer (probando candidatos): %s (%s)",
                      err.message, debug)
            return
        log.error("GStreamer: %s (%s)", err.message, debug)
        if self.on_error:
            self.on_error(err.message)

    # -------------------------------------------------------------- entrada
    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)

        pts = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else 0
        pts_us = pts // 1000

        flags = 0
        if not buf.has_flags(Gst.BufferFlags.DELTA_UNIT):
            flags |= protocol.FLAG_KEYFRAME

        try:
            if not self._sent_config and (flags & protocol.FLAG_KEYFRAME):
                config, rest = split_parameter_sets(data, self.codec)
                if config:
                    self.on_frame(protocol.FLAG_CONFIG, pts_us, config)
                    self._sent_config = True
                    data = rest
            self.on_frame(flags, pts_us, data)
        except Exception:  # pragma: no cover
            log.exception("on_frame falló")
        return Gst.FlowReturn.OK

    # ----------------------------------------------------------- adaptación
    def set_bitrate(self, kbps):
        kbps = int(kbps)
        if self.encoder is None or kbps == self.bitrate_kbps:
            return
        self.bitrate_kbps = kbps
        _set_if_present(self.encoder, "bitrate", kbps)
        _set_if_present(self.encoder, "cpb-size", max(kbps // 2, 500))

    def set_max_rate(self, fps):
        self.limiter.set_fps(fps)

    def force_keyframe(self):
        if self.encoder is None:
            return
        pad = self.encoder.get_static_pad("sink")
        if pad is None:
            return
        event = GstVideo.video_event_new_downstream_force_key_unit(
            Gst.CLOCK_TIME_NONE, Gst.CLOCK_TIME_NONE, Gst.CLOCK_TIME_NONE,
            True, 0)
        pad.send_event(event)
        self._sent_config = False

    def stop(self):
        if self.pipeline is not None:
            if self._bus_watched:
                self.pipeline.get_bus().remove_signal_watch()
                self._bus_watched = False
            self.pipeline.set_state(Gst.State.NULL)
        # El pipeline entero se va al garete, así que las sondas se van con él:
        # quitarlas a mano sobre un pad ya muerto solo produce avisos.
        self.limiter.forget()
        self.pipeline = None
        self.encoder = None
        self.appsink = None
