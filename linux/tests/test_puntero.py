"""Fija la elección de pipeline de la que depende que se vea el puntero.

El fallo de la 1.0.1 fue de negociación, no de configuración: pedíamos el vídeo
por DMABuf, y en ese camino Mutter entrega el fotograma del monitor virtual con
un `cogl_blit_framebuffer` pelado, sin repintar el cursor. La cadena completa es:

  caps con memory:DMABuf
    -> pipewiresrc añade SPA_FORMAT_VIDEO_modifier   (gstpipewireformat.c:626)
    -> Mutter ve el modifier y sirve SPA_DATA_DmaBuf (stream-src.c:1186)
    -> do_record_frame elige record_to_framebuffer   (stream-src.c:623)
    -> el stream virtual hace un blit sin cursores   (virtual-stream-src.c:422)

  caps `video/x-raw` a secas
    -> sin modifier -> SPA_DATA_MemFd -> record_to_buffer
    -> repinta con CLUTTER_PAINT_FLAG_FORCE_CURSORS  (virtual-stream-src.c:401)

Así que con puntero activado ningún pipeline puede dejar que pipewiresrc
negocie DMABuf. Esto lo comprueba aquí sin necesitar GPU ni GNOME.
"""

import sys

sys.path.insert(0, "..")

from bothscreen import encoder, protocol  # noqa: E402
from test_pipeline import check  # noqa: E402


def candidatos(want_cursor, con_va=True):
    original = encoder._find_va_encoder
    if con_va:
        encoder._find_va_encoder = lambda codec: (
            "vah264enc" if codec == protocol.CODEC_H264 else "vah265enc")
    try:
        st = encoder.Streamer(
            42, 1920, 1200, 60, protocol.CODEC_H264, 6000,
            on_frame=lambda *a: None, want_cursor=want_cursor)
        return st._candidates()
    finally:
        encoder._find_va_encoder = original


def fuerza_memoria(desc):
    """¿El capsfilter que sigue a la fuente descarta DMABuf?

    Tiene que ser `video/x-raw` sin ninguna característica de memoria: es la
    ausencia de `(memory:DMABuf)` lo que hace que pipewiresrc no anuncie
    `modifier` y Mutter sirva por memoria. Los atributos que lleve detrás
    (framerate, etc.) dan igual.
    """
    tras_fuente = desc.split("!", 1)[1].strip() if "!" in desc else ""
    return (tras_fuente.startswith("video/x-raw")
            and "(memory:" not in tras_fuente.split("!", 1)[0])


def main():
    encoder.init_gst()
    ok = True

    print("\n1) Con el puntero activado")
    lista = candidatos(want_cursor=True)
    nombres = [c[0] for c in lista]
    print("     pipelines: %s" % ", ".join(nombres))
    ok &= check("no se ofrece el camino DMABuf", "va-dmabuf" not in nombres)
    ok &= check("ningún pipeline pide memoria DMABuf en los caps",
                all("memory:DMABuf" not in c[3] for c in lista))
    ok &= check("todos fuerzan memoria de sistema tras pipewiresrc",
                all(fuerza_memoria(c[3]) for c in lista),
                str([c[0] for c in lista if not fuerza_memoria(c[3])]))
    ok &= check("el primero sigue siendo por hardware",
                lista and lista[0][1] is True and lista[0][0] == "va-memoria",
                lista[0][0] if lista else "ninguno")
    ok &= check("hay repuesto si vapostproc no traga memoria de sistema",
                "va-memoria-cpu" in nombres)
    ok &= check("y repuesto por software", "software" in nombres)

    print("\n2) Con el puntero desactivado se recupera el camino cero-copia")
    lista_sin = candidatos(want_cursor=False)
    nombres_sin = [c[0] for c in lista_sin]
    print("     pipelines: %s" % ", ".join(nombres_sin))
    ok &= check("el primero es va-dmabuf", nombres_sin[0] == "va-dmabuf")
    ok &= check("y usa VAMemory de verdad",
                "memory:VAMemory" in lista_sin[0][3])
    ok &= check("los de memoria siguen ahí como repuesto",
                "va-memoria" in nombres_sin)

    print("\n3) Sin VA-API, el camino por software también muestra el puntero")
    solo_sw = candidatos(want_cursor=True, con_va=False)
    ok &= check("solo queda software", [c[0] for c in solo_sw] == ["software"])
    ok &= check("y fuerza memoria de sistema", fuerza_memoria(solo_sw[0][3]))

    print("\n4) Los pipelines no tienen erratas")
    # No se pueden construir de verdad sin GPU, así que se valida lo que sí se
    # puede validar sin ella: que cada caps se parsee y que cada elemento
    # exista (o sea uno de VA-API, que aquí no está instalado pero en el
    # portátil sí).
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    solo_en_gpu = {"vah264enc", "vah265enc", "vapostproc"}
    for nombre, _hw, _fab, desc in candidatos(True) + candidatos(False):
        problemas = []
        for tramo in desc.split("!"):
            tramo = tramo.strip()
            if not tramo:
                continue
            if tramo.startswith("video/") or tramo.startswith("audio/"):
                caps = Gst.Caps.from_string(tramo)
                if caps is None or caps.is_empty():
                    problemas.append("caps ilegibles: %s" % tramo)
                continue
            elemento = tramo.split()[0]
            if (elemento not in solo_en_gpu
                    and not Gst.ElementFactory.find(elemento)):
                problemas.append("elemento inexistente: %s" % elemento)
        ok &= check("  %s" % nombre, not problemas, "; ".join(problemas))

    print("\n5) La sesión le pasa al codificador lo que toca")
    from bothscreen import server

    capturado = {}

    class FalsoStreamer:
        def __init__(self, *args, **kwargs):
            capturado.update(kwargs)

        def start(self):
            pass

        def set_max_rate(self, _fps):
            pass

    original = encoder.Streamer
    encoder.Streamer = FalsoStreamer
    try:
        cfg = server.Config()
        sesion = server.Session.__new__(server.Session)
        sesion.cfg = cfg
        sesion.codec = protocol.CODEC_H264
        sesion.width, sesion.height, sesion.max_rate = 1920, 1200, 60
        sesion.bitrate = 6000
        sesion._on_frame = lambda *a: None
        sesion._on_error = lambda *a: None
        # se replica la única línea que interesa de Session.start()
        streamer = encoder.Streamer(
            0, sesion.width, sesion.height, sesion.max_rate, sesion.codec,
            sesion.bitrate, on_frame=sesion._on_frame,
            on_error=sesion._on_error,
            prefer_hardware=cfg.prefer_hardware,
            rate_control=cfg.rate_control,
            want_cursor=cfg.cursor_mode != 0)
        assert isinstance(streamer, FalsoStreamer)
    finally:
        encoder.Streamer = original

    ok &= check("por defecto el puntero está activado",
                capturado.get("want_cursor") is True)

    print("\n%s" % ("TODAS LAS PRUEBAS PASARON" if ok else "HAY FALLOS"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
