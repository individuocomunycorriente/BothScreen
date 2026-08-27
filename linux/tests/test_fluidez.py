"""Fija lo que hace que una actualización pequeña y aislada llegue a la tablet.

El síntoma de la 1.0.2 era «solo se actualiza si hay mucho movimiento»: un
puntero cruzando un escritorio quieto produce fotogramas sueltos, muy separados
entre sí, y esos son justo los que el pipeline perdía. Tres causas, las tres
mías:

1. `videorate` delante de una fuente guiada por daño. Además de meterse en la
   negociación de caps, es un elemento más que puede quedarse con un fotograma
   aislado. Sustituido por una sonda que decide en el acto.
2. El framerate sin fijar en los caps. Mutter usa el `max_framerate` que negocie
   el cliente para decidir cada cuánto graba (meta-screen-cast-stream-src.c,
   `maybe_record_frame`), así que dejarlo a la negociación es jugársela.
3. `always-copy=false` en pipewiresrc. Mutter tiene un juego pequeño de buffers;
   si al ir a grabar no encuentra ninguno libre, NO graba y no reprograma nada
   (`pw_stream_dequeue_buffer` devuelve NULL y se sale). Con always-copy=false
   el buffer recorre todo el pipeline antes de volver, así que un fotograma
   suelto se puede perder para siempre.
"""

import sys

sys.path.insert(0, "..")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from bothscreen import encoder, protocol  # noqa: E402
from test_pipeline import check  # noqa: E402
from test_puntero import candidatos  # noqa: E402

MS = Gst.MSECOND


def main():
    encoder.init_gst()
    ok = True

    print("\n1) El limitador nunca retiene un fotograma")
    lim = encoder._RateLimiter(60)          # intervalo mínimo 16,6 ms
    ok &= check("el primero pasa", lim.should_pass(0))
    ok &= check("uno a 5 ms se descarta", not lim.should_pass(5 * MS))
    ok &= check("uno a 10 ms se descarta", not lim.should_pass(10 * MS))
    ok &= check("uno a 20 ms pasa", lim.should_pass(20 * MS))

    # Lo que de verdad importa: tras un silencio largo, el fotograma aislado
    # sale inmediatamente en vez de esperar a que llegue el siguiente.
    ok &= check("tras 5 s de nada, el fotograma suelto pasa al instante",
                lim.should_pass(5 * Gst.SECOND))
    ok &= check("y el siguiente suelto, 3 s después, también",
                lim.should_pass(8 * Gst.SECOND))

    lim2 = encoder._RateLimiter(60)
    sueltos = [lim2.should_pass(i * Gst.SECOND) for i in range(10)]
    ok &= check("diez actualizaciones espaciadas pasan todas",
                all(sueltos), str(sueltos))

    print("\n2) El tope se puede cambiar en caliente")
    lim3 = encoder._RateLimiter(60)
    lim3.should_pass(0)
    ok &= check("a 60 fps, 20 ms pasa", lim3.should_pass(20 * MS))
    lim3.set_fps(10)                        # intervalo mínimo 100 ms
    ok &= check("a 10 fps, 60 ms se descarta", not lim3.should_pass(60 * MS))
    ok &= check("a 10 fps, 130 ms pasa", lim3.should_pass(130 * MS))

    print("\n3) El reloj hacia atrás no lo bloquea")
    lim4 = encoder._RateLimiter(60)
    lim4.should_pass(10 * Gst.SECOND)
    ok &= check("un pts anterior se acepta (pipeline reiniciado)",
                lim4.should_pass(0))

    print("\n4) Los pipelines no vuelven a las andadas")
    todos = candidatos(True) + candidatos(False)
    ok &= check("ninguno usa videorate",
                all("videorate" not in c[3] for c in todos))
    ok &= check("todos copian el buffer de PipeWire de entrada",
                all("always-copy=true" in c[3] for c in todos))
    ok &= check("ninguno deja always-copy=false",
                all("always-copy=false" not in c[3] for c in todos))
    # Exigirle un framerate concreto a pipewiresrc rompe la negociación: Mutter
    # anuncia 0/1 (variable, porque el stream va guiado por daño) y el pipeline
    # se queda en "no more input formats". La cadencia se controla en el
    # refresh-rate del monitor virtual, no aquí.
    ok &= check("ninguno le exige un framerate a pipewiresrc",
                all("framerate=" not in c[3].split("!")[1] for c in todos),
                str([c[0] for c in todos
                     if "framerate=" in c[3].split("!")[1]]))

    print("\n5) El limitador funciona dentro de un pipeline real")
    import time
    from test_pipeline import TestStreamer

    llegadas = []
    st = TestStreamer(0, 640, 480, 30, protocol.CODEC_H264, 3000,
                      on_frame=lambda f, p, d: llegadas.append(time.monotonic()))
    st.start()
    time.sleep(2.0)
    sin_tope = len(llegadas)
    llegadas.clear()
    st.set_max_rate(5)
    time.sleep(2.0)
    con_tope = len(llegadas)
    st.stop()

    ok &= check("sin tope llegan bastantes", sin_tope > 20,
                "%d en 2 s" % sin_tope)
    ok &= check("con tope de 5 fps llegan muchos menos",
                con_tope < sin_tope / 2, "%d en 2 s" % con_tope)
    ok &= check("pero siguen llegando", con_tope >= 5, "%d" % con_tope)

    print("\n6) Un fotograma aislado sale enseguida, no cuando llega el siguiente")
    # Esta es la prueba que de verdad reproduce el síntoma. Se deja pasar un
    # fotograma cada medio segundo y se mide cuánto tarda cada uno en salir
    # codificado. Si el parser o el codificador retuvieran el fotograma a la
    # espera del siguiente, la latencia sería de ~500 ms en vez de unos pocos.
    entradas = []
    latencias = []

    def al_salir(_flags, _pts, _datos):
        if entradas:
            latencias.append(time.monotonic() - entradas[-1])

    st2 = TestStreamer(0, 640, 480, 30, protocol.CODEC_H264, 3000,
                       on_frame=al_salir)
    st2.start()
    cola = st2.pipeline.get_by_name("q")
    cola.get_static_pad("src").add_probe(
        Gst.PadProbeType.BUFFER,
        lambda _p, _i: (entradas.append(time.monotonic()),
                        Gst.PadProbeReturn.OK)[1])
    st2.set_max_rate(2)                     # uno cada 500 ms
    time.sleep(4.0)
    st2.stop()

    utiles = latencias[1:]                  # el primero arrastra el arranque
    peor = max(utiles) if utiles else None
    ok &= check("se codificaron varios fotogramas aislados",
                len(utiles) >= 3, "%d" % len(utiles))
    ok &= check("ninguno esperó al siguiente",
                peor is not None and peor < 0.25,
                "peor latencia %.0f ms" % (peor * 1000) if peor else "sin datos")

    print("\n7) Un candidato que falla no se lleva la sesión por delante")
    # En la 1.0.3, el error de bus del primer candidato llegaba a on_error, que
    # cierra la sesión y retira el monitor virtual; los candidatos siguientes se
    # quedaban entonces sin nada que capturar y fallaban también. El buscador
    # tiene que tragarse esos errores mientras busca.
    avisos = []

    class ConCandidatoRoto(TestStreamer):
        def _candidates(self):
            bueno = TestStreamer._candidates(self)[0]
            # Se construye bien pero revienta al pasar a PLAYING, que es
            # justo cuando el bus emite el error que hay que tragarse.
            roto = ("roto", False, "identity",
                    "filesrc location=/no/existe/de/verdad ! "
                    "queue name=q ! identity name=venc ! "
                    "identity name=parse ! "
                    "appsink name=sink emit-signals=true sync=false")
            return [roto, bueno]

    st3 = ConCandidatoRoto(0, 320, 240, 30, protocol.CODEC_H264, 2000,
                           on_frame=lambda *a: None,
                           on_error=lambda msg: avisos.append(msg))
    arrancado, detalle = True, ""
    try:
        st3.start()
    except Exception as exc:
        arrancado, detalle = False, str(exc)
    activo = st3.description
    st3.stop()

    ok &= check("se acabó arrancando el candidato bueno",
                arrancado and activo == "test", detalle or activo)
    ok &= check("el fallo del roto no llegó a on_error", not avisos,
                str(avisos))

    print("\n%s" % ("TODAS LAS PRUEBAS PASARON" if ok else "HAY FALLOS"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
