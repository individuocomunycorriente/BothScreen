"""Servidor TCP: negocia con la tablet, arranca la captura y adapta el caudal.

Una sola sesión activa a la vez. Mientras no hay tablet conectada no existe
monitor virtual, así que el escritorio queda exactamente como estaba.
"""

import collections
import logging
import select
import socket
import threading
import time

from . import encoder, protocol
from .virtualmonitor import VirtualMonitor, CURSOR_MODE_EMBEDDED

log = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 8.0
THREAD_JOIN_TIMEOUT = 3.0


class Config:
    """Parámetros de la transmisión. Todo tiene un valor por defecto sensato."""

    def __init__(self):
        self.port = 27183
        self.max_width = 1920          # lado largo de la pantalla virtual
        self.max_height = 1200
        self.fps = 60
        self.min_bitrate = 1500        # kbps
        self.max_bitrate = 14000       # kbps
        self.start_bitrate = 6000      # kbps
        self.prefer_hevc = True        # HEVC gasta ~35 % menos a igual calidad
        self.prefer_hardware = True
        self.rate_control = "vbr"
        self.cursor_mode = CURSOR_MODE_EMBEDDED
        # Mutter solo compone el puntero dentro del vídeo cuando el monitor
        # virtual NO se declara como monitor de plataforma. `is-platform` está
        # pensado para sesiones headless, donde la pantalla virtual sustituye a
        # la real; aquí es una pantalla más y el valor correcto es el que Mutter
        # usa por defecto.
        self.is_platform = False
        self.adaptive = True


def _round8(value):
    return max(8, int(value) // 8 * 8)


def negotiate_resolution(cfg, tablet_w, tablet_h):
    """Respeta la relación de aspecto del panel y limita el lado largo.

    La Tab S7 es 2560x1600 (16:10). Con el tope 1920x1200 sale exactamente esa
    relación, así que la imagen llena la pantalla sin bandas y el escalado a
    nativo lo hace la GPU de la tablet, que es gratis.
    """
    if not tablet_w or not tablet_h:
        return cfg.max_width, cfg.max_height
    if tablet_w < tablet_h:  # la app va en horizontal; corrige si informó al revés
        tablet_w, tablet_h = tablet_h, tablet_w
    scale = min(cfg.max_width / tablet_w, cfg.max_height / tablet_h, 1.0)
    return _round8(tablet_w * scale), _round8(tablet_h * scale)


class _Sender(threading.Thread):
    """Cola acotada hacia el socket, con política de descarte por GOP.

    Si la cola se llena hay congestión real: tirar frames sueltos dejaría el
    decodificador con basura hasta el siguiente keyframe, así que se vacía todo
    y se pide un keyframe inmediato. Se pierde medio parpadeo en lugar de
    varios segundos de artefactos.
    """

    def __init__(self, sock, on_overflow, maxlen=8):
        super().__init__(name="sender", daemon=True)
        self.sock = sock
        self.on_overflow = on_overflow
        self.queue = collections.deque()
        self.maxlen = maxlen
        self.cond = threading.Condition()
        self.running = True
        self.bytes_sent = 0
        self.frames_sent = 0

    def put(self, payload, is_frame=True):
        with self.cond:
            if is_frame and len(self.queue) >= self.maxlen:
                self.queue.clear()
                self.cond.notify()
                if self.on_overflow:
                    self.on_overflow()
                return False
            self.queue.append(payload)
            self.cond.notify()
            return True

    def run(self):
        while self.running:
            with self.cond:
                while self.running and not self.queue:
                    self.cond.wait(0.5)
                if not self.running:
                    return
                chunk = self.queue.popleft()
            try:
                self.sock.sendall(chunk)
            except OSError as exc:
                log.info("socket cerrado al enviar: %s", exc)
                self.running = False
                return
            self.bytes_sent += len(chunk)
            self.frames_sent += 1

    def stop(self):
        with self.cond:
            self.running = False
            self.queue.clear()
            self.cond.notify_all()


class Session:
    """Una tablet conectada: monitor virtual + pipeline + adaptación."""

    def __init__(self, sock, cfg, on_state=None):
        self.sock = sock
        self.cfg = cfg
        self.on_state = on_state
        self.monitor = None
        self.streamer = None
        self.sender = None
        self.width = cfg.max_width
        self.height = cfg.max_height
        self.codec = protocol.CODEC_H264
        self.bitrate = cfg.start_bitrate
        self.max_rate = cfg.fps
        self.running = False

        self._frames_out = 0
        self._frames_acked = 0
        self._last_ack_pts = 0
        self._stable_ticks = 0
        self._stats = {"bitrate": 0.0, "fps": 0.0, "in_flight": 0}
        self._threads = []
        self._stop_lock = threading.Lock()
        self._stopped = False

    # ------------------------------------------------------------- handshake
    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("la tablet cerró la conexión")
            buf += chunk
        return buf

    def _handshake(self):
        # Con timeout: un cliente que se conecta y no dice nada dejaría el hilo
        # de accept bloqueado para siempre, y con él el puerto ocupado.
        self.sock.settimeout(HANDSHAKE_TIMEOUT)
        try:
            hello = protocol.parse_hello(
                self._read_exact(protocol.HELLO_FIXED.size))
            name = self._read_exact(hello["name_len"]).decode("utf-8", "replace")
        finally:
            self.sock.settimeout(None)
        log.info("tablet conectada: %s (%dx%d, %d fps, códecs 0x%x)",
                 name, hello["width"], hello["height"], hello["max_fps"],
                 hello["codecs"])

        pc_codecs = encoder.available_codecs()
        shared = pc_codecs & hello["codecs"]
        if not shared:
            raise RuntimeError("no hay ningún códec en común con la tablet")
        if self.cfg.prefer_hevc and (shared & protocol.CODEC_BIT_HEVC):
            self.codec = protocol.CODEC_HEVC
        else:
            self.codec = protocol.CODEC_H264

        self.width, self.height = negotiate_resolution(
            self.cfg, hello["width"], hello["height"])
        self.max_rate = min(self.cfg.fps, hello["max_fps"] or self.cfg.fps)
        return name

    # ------------------------------------------------------------------ run
    def start(self):
        name = self._handshake()

        self.monitor = VirtualMonitor(
            self.width, self.height, refresh_rate=float(self.max_rate),
            cursor_mode=self.cfg.cursor_mode, is_platform=self.cfg.is_platform)
        node_id = self.monitor.start()

        self.sender = _Sender(self.sock, self._on_overflow)
        self.sender.start()

        self.sock.sendall(protocol.pack_config(
            self.codec, self.width, self.height, self.max_rate, self.bitrate))

        # Ojo con el orden: el pipeline empieza a soltar buffers dentro de
        # start(), así que hay que aceptar frames antes de arrancarlo. Si no, se
        # pierden la CSD y el primer keyframe, y la tablet se queda en negro
        # hasta el siguiente refresco intra.
        self.running = True

        self.streamer = encoder.Streamer(
            node_id, self.width, self.height, self.max_rate, self.codec,
            self.bitrate, on_frame=self._on_frame, on_error=self._on_error,
            prefer_hardware=self.cfg.prefer_hardware,
            rate_control=self.cfg.rate_control,
            want_cursor=self.cfg.cursor_mode != 0)
        self.streamer.start()
        self.streamer.set_max_rate(self.max_rate)

        for target, name_ in ((self._reader, "ctl-reader"),
                              (self._adapt, "adaptador")):
            thread = threading.Thread(target=target, name=name_, daemon=True)
            self._threads.append(thread)
            thread.start()
        self._notify()
        log.info("transmitiendo %dx%d@%d %s por %s",
                 self.width, self.height, self.max_rate,
                 "HEVC" if self.codec == protocol.CODEC_HEVC else "H.264",
                 self.streamer.description)
        return name

    def _notify(self):
        if self.on_state:
            try:
                self.on_state(self.describe())
            except Exception:
                log.exception("callback de estado falló")

    def describe(self):
        return {
            "running": self.running,
            "width": self.width,
            "height": self.height,
            "fps": self.max_rate,
            "codec": "HEVC" if self.codec == protocol.CODEC_HEVC else "H.264",
            "hardware": bool(self.streamer and self.streamer.hardware),
            "pipeline": self.streamer.description if self.streamer else "",
            "bitrate_target": self.bitrate,
            "bitrate_actual": self._stats["bitrate"],
            "fps_actual": self._stats["fps"],
            "in_flight": self._stats["in_flight"],
        }

    # --------------------------------------------------------------- eventos
    def _on_frame(self, flags, pts_us, data):
        if not self.running:
            return
        self._frames_out += 1
        self.sender.put(protocol.pack_frame(flags, pts_us, data))

    def _on_overflow(self):
        log.debug("cola llena: descarto GOP y pido keyframe")
        if self.streamer:
            self.streamer.force_keyframe()

    def _on_error(self, message):
        log.error("error de pipeline: %s", message)
        self.stop()

    def _reader(self):
        """Lee ACKs de la tablet: es nuestra única señal de latencia real."""
        self.sock.settimeout(1.0)
        buf = b""
        while self.running:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while buf:
                kind = buf[0]
                if kind == protocol.CTL_ACK:
                    if len(buf) < 1 + protocol.ACK_STRUCT.size:
                        break
                    (pts,) = protocol.ACK_STRUCT.unpack_from(buf, 1)
                    buf = buf[1 + protocol.ACK_STRUCT.size:]
                    self._frames_acked += 1
                    self._last_ack_pts = pts
                elif kind == protocol.CTL_REQUEST_KEYFRAME:
                    buf = buf[1:]
                    if self.streamer:
                        self.streamer.force_keyframe()
                else:
                    log.warning("mensaje de control desconocido: %d", kind)
                    buf = b""
        log.info("la tablet se desconectó")
        self.stop()

    # ------------------------------------------------------------ adaptación
    def _adapt(self):
        """Controlador AIMD sobre los frames en vuelo.

        `in_flight` = enviados - confirmados. Si crece, el cuello de botella
        está en el enlace o en el decodificador, y hay que bajar. Si se mantiene
        en 0-1 durante un par de segundos, sobra capacidad y se sube despacio.
        """
        tick = 0.5
        last_bytes = 0
        last_frames = 0
        last_time = time.monotonic()

        while self.running:
            time.sleep(tick)
            if not self.running or self.sender is None:
                break

            now = time.monotonic()
            elapsed = max(now - last_time, 1e-6)
            sent = self.sender.bytes_sent
            frames = self.sender.frames_sent
            measured_kbps = (sent - last_bytes) * 8 / elapsed / 1000.0
            measured_fps = (frames - last_frames) / elapsed
            last_bytes, last_frames, last_time = sent, frames, now

            in_flight = max(self._frames_out - self._frames_acked, 0)
            self._stats = {"bitrate": measured_kbps, "fps": measured_fps,
                           "in_flight": in_flight}

            if self.cfg.adaptive:
                if in_flight > 4:
                    self._stable_ticks = 0
                    target = max(self.cfg.min_bitrate, int(self.bitrate * 0.7))
                    if in_flight > 8 and self.max_rate > 24:
                        self.max_rate = max(24, self.max_rate - 10)
                        self.streamer.set_max_rate(self.max_rate)
                    if target != self.bitrate:
                        self.bitrate = target
                        self.streamer.set_bitrate(target)
                        log.debug("congestión (%d en vuelo) -> %d kbps @%d fps",
                                  in_flight, target, self.max_rate)
                elif in_flight <= 1:
                    self._stable_ticks += 1
                    if self._stable_ticks >= 4:
                        self._stable_ticks = 0
                        if self.max_rate < min(self.cfg.fps, 60):
                            self.max_rate = min(self.cfg.fps, self.max_rate + 10)
                            self.streamer.set_max_rate(self.max_rate)
                        target = min(self.cfg.max_bitrate,
                                     int(self.bitrate * 1.15) + 250)
                        if target != self.bitrate:
                            self.bitrate = target
                            self.streamer.set_bitrate(target)
                else:
                    self._stable_ticks = 0

            try:
                self.sender.put(
                    protocol.pack_stats(int(measured_kbps), measured_fps),
                    is_frame=False)
            except Exception:
                pass
            self._notify()

    # ------------------------------------------------------------------ stop
    def stop(self):
        """Cierra todo y no deja ni un hilo ni un descriptor vivo.

        Es reentrante y se puede llamar desde cualquier hilo, incluidos los que
        ella misma tiene que esperar: a esos no se les hace join, obviamente.
        """
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self.running = False

        if self.streamer:
            self.streamer.stop()
            self.streamer = None
        sender = self.sender
        if sender:
            sender.stop()
            self.sender = None
        if self.monitor:
            self.monitor.stop()
            self.monitor = None

        # Cerrar el socket despierta tanto al lector, que está en recv, como al
        # emisor, si se quedó bloqueado en un sendall hacia una tablet muerta.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

        current = threading.current_thread()
        if sender is not None and sender is not current and sender.is_alive():
            sender.join(THREAD_JOIN_TIMEOUT)
        for thread in self._threads:
            if thread is not current and thread.is_alive():
                thread.join(THREAD_JOIN_TIMEOUT)
                if thread.is_alive():
                    log.warning("el hilo %s no terminó a tiempo", thread.name)
        self._threads = []

        self._notify()
        log.info("sesión terminada, monitor virtual retirado")


class DisplayServer:
    """Escucha en localhost y atiende a una tablet cada vez.

    El punto delicado es apagarlo. Cerrar el socket de escucha desde otro hilo
    NO despierta a quien está bloqueado en accept(), y mientras ese accept siga
    vivo el puerto sigue ocupado (SO_REUSEADDR no permite dos escuchas a la vez)
    y, peor todavía, la siguiente conexión que llegue se atiende igual aunque el
    servidor esté "parado". Por eso hay un socketpair que sirve de timbre: se
    espera con select en el socket y en el timbre a la vez, y stop() toca el
    timbre y espera a que el hilo muera de verdad antes de dar por cerrado.
    """

    def __init__(self, cfg, on_state=None):
        self.cfg = cfg
        self.on_state = on_state
        self.sock = None
        self.session = None
        self.running = False
        self._thread = None
        self._wake_r = None
        self._wake_w = None
        self._stop_lock = threading.Lock()

    def start(self):
        self._wake_r, self._wake_w = socket.socketpair()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", self.cfg.port))
            sock.listen(1)
        except OSError:
            sock.close()
            self._close_wakeup()
            raise
        self.sock = sock
        self.running = True
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="accept", daemon=True)
        self._thread.start()
        log.info("escuchando en 127.0.0.1:%d", self.cfg.port)

    def _accept_loop(self):
        while self.running:
            try:
                ready, _, _ = select.select([self.sock, self._wake_r], [], [])
            except (OSError, ValueError):
                break
            if not self.running or self._wake_r in ready:
                break
            try:
                conn, addr = self.sock.accept()
            except OSError:
                break

            if not self.running:
                conn.close()
                break
            if self.session is not None and self.session.running:
                log.warning("ya hay una tablet conectada; rechazo %s", addr)
                conn.close()
                continue

            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
            session = Session(conn, self.cfg, self.on_state)
            self.session = session
            try:
                session.start()
            except Exception as exc:
                log.error("no se pudo iniciar la sesión: %s", exc)
                session.stop()
                self.session = None
        log.debug("bucle de aceptación terminado")

    def _close_wakeup(self):
        for sock in (self._wake_r, self._wake_w):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._wake_r = self._wake_w = None

    def stop(self):
        with self._stop_lock:
            if not self.running and self.sock is None:
                return
            self.running = False

        if self.session:
            self.session.stop()
            self.session = None

        if self._wake_w is not None:
            try:
                self._wake_w.send(b"x")
            except OSError:
                pass

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(THREAD_JOIN_TIMEOUT)
            if self._thread.is_alive():
                log.warning("el hilo de aceptación no terminó a tiempo")
        self._thread = None

        # Solo ahora se cierra el socket: si se cerrara antes, el hilo podría
        # seguir dentro de accept() sujetando el puerto.
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self._close_wakeup()
        log.info("servidor detenido, puerto %d liberado", self.cfg.port)
