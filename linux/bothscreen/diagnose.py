"""Comprueba, en tu propia máquina, qué camino de captura muestra el puntero.

Mutter entrega el fotograma de dos formas distintas y elige según lo que negocie
el cliente. Con buffers DMABuf usa `record_to_framebuffer`, que para un monitor
virtual es un blit pelado sin tratamiento del cursor; con buffers en memoria usa
`record_to_buffer`, que repinta la escena con FORCE_CURSORS. Por eso el mismo
`cursor-mode` da resultados distintos según cómo se pida el vídeo.

La prueba: por cada camino se captura un fotograma con el cursor oculto y otro
con el cursor incrustado, y se comparan. Si la única diferencia es una manchita
compacta de unos cientos de píxeles, ese es el puntero.
"""

import logging
import os
import struct
import time
import zlib

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from . import encoder
from .virtualmonitor import (VirtualMonitor, CURSOR_MODE_EMBEDDED,
                             CURSOR_MODE_HIDDEN)

log = logging.getLogger(__name__)

ANCHO = 960
ALTO = 600


def _cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "bothscreen")
    os.makedirs(path, exist_ok=True)
    return path


def _write_png(path, rgb, width, height):
    raw = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3]
                   for y in range(height))

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR",
                       struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        fh.write(chunk(b"IEND", b""))


def _capture(node_id, seconds, forzar_memoria):
    """Devuelve los bytes RGB del último fotograma capturado.

    `forzar_memoria` pone un capsfilter `video/x-raw` sin características justo
    detrás de pipewiresrc. Eso es lo que decide todo: sin la característica
    memory:DMABuf, pipewiresrc no anuncia `modifier` y Mutter sirve el vídeo por
    memoria en vez de por DMABuf.
    """
    filtro = "video/x-raw ! " if forzar_memoria else ""
    desc = (
        "pipewiresrc path={node} do-timestamp=true ! {filtro}"
        "videorate max-rate=10 ! videoconvertscale ! "
        "video/x-raw,format=RGB,width={w},height={h} ! "
        "appsink name=sink emit-signals=false sync=false max-buffers=2 "
        "drop=true"
    ).format(node=node_id, filtro=filtro, w=ANCHO, h=ALTO)

    pipeline = Gst.parse_launch(desc)
    sink = pipeline.get_by_name("sink")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            sample = sink.emit("try-pull-sample", 500 * Gst.MSECOND)
            if sample is None:
                continue
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if ok:
                last = bytes(info.data)
                buf.unmap(info)
        return last
    finally:
        pipeline.set_state(Gst.State.NULL)


def _diferencia(a, b):
    """Cuenta píxeles distintos entre dos fotogramas RGB del mismo tamaño."""
    if a is None or b is None or len(a) != len(b):
        return None
    distintos = 0
    for i in range(0, len(a), 3):
        if (abs(a[i] - b[i]) + abs(a[i + 1] - b[i + 1])
                + abs(a[i + 2] - b[i + 2])) > 30:
            distintos += 1
    return distintos


def _probar(forzar_memoria, segundos):
    resultados = {}
    for nombre, modo in (("oculto", CURSOR_MODE_HIDDEN),
                         ("incrustado", CURSOR_MODE_EMBEDDED)):
        monitor = VirtualMonitor(ANCHO, ALTO, refresh_rate=30.0,
                                 cursor_mode=modo, is_platform=False)
        try:
            node = monitor.start()
            resultados[nombre] = _capture(node, segundos, forzar_memoria)
        finally:
            monitor.stop()
        time.sleep(0.5)
    return resultados


def run(segundos=5):
    encoder.init_gst()
    destino = _cache_dir()

    print()
    print("  Diagnóstico del puntero")
    print("  " + "-" * 60)
    print("  Se va a crear una pantalla virtual pequeña cuatro veces seguidas.")
    print("  Dos capturas por cada camino de captura (memoria y DMABuf).")
    print("  Aparecerá en Configuración → Pantallas, normalmente a la derecha.")
    print()
    print("  IMPORTANTE: mueve el ratón hasta esa pantalla y DÉJALO QUIETO ahí")
    print("  hasta que termine la prueba (unos %d segundos)." % (segundos * 4 + 6))
    print()
    for i in (3, 2, 1):
        print("    empezando en %d…" % i)
        time.sleep(1)

    veredictos = []
    for etiqueta, forzar_memoria in (("memoria", True), ("dmabuf", False)):
        print("\n  Probando la captura por %s…" % etiqueta)
        try:
            capturas = _probar(forzar_memoria, segundos)
        except Exception as exc:
            print("    no se pudo probar este camino: %s" % exc)
            continue

        for nombre, datos in capturas.items():
            if datos is None:
                continue
            ruta = os.path.join(destino, "puntero-%s-%s.png" % (etiqueta, nombre))
            _write_png(ruta, datos, ANCHO, ALTO)
            print("    guardado %s" % ruta)

        distintos = _diferencia(capturas.get("oculto"),
                                capturas.get("incrustado"))
        if distintos is None:
            print("    sin datos suficientes")
            continue
        total = ANCHO * ALTO
        if 40 <= distintos <= total * 0.05:
            print("    ✓ el puntero SÍ se incrusta (%d píxeles de diferencia)"
                  % distintos)
            veredictos.append(etiqueta)
        elif distintos < 40:
            print("    ✗ el puntero no aparece (%d píxeles de diferencia)"
                  % distintos)
        else:
            print("    ? la pantalla cambió demasiado durante la prueba "
                  "(%d píxeles); repítela sin tocar nada" % distintos)

    print("\n  " + "-" * 60)
    if not veredictos:
        print("  Ningún camino mostró el puntero.")
        print("  Mira los PNG guardados en %s para confirmarlo a ojo." % destino)
        print("  Puede ser que el ratón no llegara a estar sobre la pantalla")
        print("  virtual; en ese caso vuelve a intentarlo.")
        return 1

    if "memoria" in veredictos:
        print("  El camino por memoria muestra el puntero, que es justo el que")
        print("  usa la aplicación cuando el puntero está activado. No hay nada")
        print("  que cambiar: arranca normal.")
    else:
        print("  Curioso: aquí funciona el camino DMABuf y no el de memoria,")
        print("  al revés de lo habitual. Arranca con --no-cursor para forzar")
        print("  DMABuf, y cuéntamelo, porque es al contrario de lo que hace")
        print("  el código de Mutter que revisé.")
    return 0
