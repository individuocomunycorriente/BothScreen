"""Un único bucle GLib compartido, en su propio hilo.

Tanto las señales D-Bus de Mutter como el bus de GStreamer necesitan que alguien
itere el contexto principal de GLib. El servidor TCP, en cambio, vive en hilos
normales de Python. Este módulo levanta el bucle una sola vez y deja que el
resto del programa trabaje con hilos corrientes.
"""

import threading

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

_loop = None
_thread = None
_lock = threading.Lock()


def start():
    global _loop, _thread
    with _lock:
        if _loop is not None:
            return _loop
        _loop = GLib.MainLoop()
        started = threading.Event()

        def run():
            GLib.idle_add(lambda: (started.set(), False)[1])
            _loop.run()

        _thread = threading.Thread(target=run, name="glib-mainloop", daemon=True)
        _thread.start()
        started.wait(5)
        return _loop


def stop():
    global _loop, _thread
    with _lock:
        if _loop is not None:
            _loop.quit()
        _loop = None
        _thread = None
