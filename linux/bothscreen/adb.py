"""Envoltorio mínimo sobre adb: túnel inverso por USB y arranque de la app.

`adb reverse tcp:P tcp:P` hace que el puerto P del *teléfono* apunte al puerto P
del *PC*. Así la tablet se conecta a 127.0.0.1 y el tráfico viaja por el cable
USB-C sin tocar la red. Es el mismo mecanismo que usa scrcpy y da del orden de
cientos de Mbit/s por USB 3.
"""

import logging
import shutil
import socket
import subprocess

log = logging.getLogger(__name__)

PACKAGE = "cl.danko.bothscreen"
ACTIVITY = "%s/.MainActivity" % PACKAGE

# La app se llamaba antes «Segunda Pantalla» y tenía otro applicationId, así que
# Android la considera una app distinta y se quedaría instalada para siempre.
# Se desinstala sola al conectar, para no dejar dos iconos que hacen lo mismo.
LEGACY_PACKAGES = ("cl.danko.segundapantalla",)

ADB_SERVER_PORT = 5037

# adb deja corriendo un demonio propio en segundo plano la primera vez que se
# le llama, y ahí se queda para siempre. Si ese demonio ya existía antes de que
# nosotros apareciéramos es de otra herramienta y no se toca; si lo levantamos
# nosotros, lo apagamos al salir para no dejar procesos sueltos.
_we_started_the_server = None


class AdbError(RuntimeError):
    pass


def adb_path():
    return shutil.which("adb")


def _server_is_running():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.4)
    try:
        probe.connect(("127.0.0.1", ADB_SERVER_PORT))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _note_server_ownership():
    global _we_started_the_server
    if _we_started_the_server is None:
        _we_started_the_server = not _server_is_running()
        if _we_started_the_server:
            log.debug("no había servidor adb; lo levantamos nosotros")


def _run(args, timeout=10, check=True):
    adb = adb_path()
    if adb is None:
        raise AdbError("adb no está instalado (sudo apt install adb)")
    _note_server_ownership()
    proc = subprocess.run([adb] + args, capture_output=True, text=True,
                          timeout=timeout)
    if check and proc.returncode != 0:
        raise AdbError("adb %s falló: %s" % (" ".join(args),
                                             (proc.stderr or proc.stdout).strip()))
    return proc.stdout.strip()


def devices():
    """Lista [(serial, estado)] de dispositivos conectados."""
    out = _run(["devices"], check=False)
    result = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            result.append((parts[0], parts[1]))
    return result


def usb_device():
    """Devuelve el serial del único dispositivo USB autorizado, o None."""
    ready = [s for s, state in devices() if state == "device"]
    if not ready:
        return None
    return ready[0]


def wait_for_device(timeout=60):
    _run(["wait-for-device"], timeout=timeout)
    return usb_device()


def reverse(port, serial=None):
    args = (["-s", serial] if serial else []) + [
        "reverse", "tcp:%d" % port, "tcp:%d" % port]
    _run(args)
    log.info("túnel inverso activo: tablet 127.0.0.1:%d -> PC 127.0.0.1:%d",
            port, port)


def remove_reverse(port, serial=None):
    args = (["-s", serial] if serial else []) + ["reverse", "--remove",
                                                 "tcp:%d" % port]
    try:
        _run(args, check=False)
    except AdbError:
        pass


def is_app_installed(serial=None):
    args = (["-s", serial] if serial else []) + [
        "shell", "pm", "list", "packages", PACKAGE]
    try:
        return PACKAGE in _run(args, check=False)
    except AdbError:
        return False


def remove_legacy_apps(serial=None):
    """Quita versiones antiguas con otro nombre de paquete."""
    for package in LEGACY_PACKAGES:
        args = (["-s", serial] if serial else []) + [
            "shell", "pm", "list", "packages", package]
        try:
            if package not in _run(args, check=False):
                continue
            log.info("desinstalando la versión antigua (%s)", package)
            _run((["-s", serial] if serial else []) + ["uninstall", package],
                 timeout=60, check=False)
        except AdbError as exc:
            log.debug("no se pudo quitar %s: %s", package, exc)


def install_apk(apk_path, serial=None):
    args = (["-s", serial] if serial else []) + ["install", "-r", "-g", apk_path]
    return _run(args, timeout=180)


def launch_app(port, serial=None):
    args = (["-s", serial] if serial else []) + [
        "shell", "am", "start", "-n", ACTIVITY,
        "--ei", "port", str(port),
        "-a", "android.intent.action.MAIN",
    ]
    return _run(args, check=False)


def stop_app(serial=None):
    args = (["-s", serial] if serial else []) + ["shell", "am", "force-stop",
                                                 PACKAGE]
    return _run(args, check=False)


def shutdown():
    """Apaga el servidor adb, pero solo si lo habíamos levantado nosotros.

    Se llama al salir del programa. Si el usuario ya tenía adb corriendo para
    otra cosa (Android Studio, otro scrcpy), no se le mata su sesión.
    """
    global _we_started_the_server
    if not _we_started_the_server:
        return
    adb = adb_path()
    if adb is None:
        return
    try:
        subprocess.run([adb, "kill-server"], capture_output=True, timeout=5)
        log.info("servidor adb apagado")
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("no se pudo apagar el servidor adb: %s", exc)
    _we_started_the_server = None


def screen_size(serial=None):
    """Resolución física del panel, para elegir la resolución de transmisión."""
    args = (["-s", serial] if serial else []) + ["shell", "wm", "size"]
    try:
        out = _run(args, check=False)
    except AdbError:
        return None
    for line in out.splitlines():
        if ":" in line:
            value = line.split(":", 1)[1].strip()
            if "x" in value:
                try:
                    w, h = value.split("x")
                    return int(w), int(h)
                except ValueError:
                    continue
    return None
