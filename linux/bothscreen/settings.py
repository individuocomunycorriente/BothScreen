"""Ajustes que sobreviven entre sesiones.

Un JSON pequeño en ~/.config/bothscreen/config.json. Solo se guardan las
opciones que el usuario toca en la ventana; todo lo demás sigue viniendo de los
valores por defecto del código, así que un archivo viejo nunca puede fijar algo
que ya no exista.
"""

import json
import logging
import os

log = logging.getLogger(__name__)

GUARDABLES = (
    "max_width", "max_height", "fps", "prefer_hevc", "adaptive",
    "cursor_mode", "is_platform", "max_bitrate", "port",
)


def _config_base():
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


def config_path():
    return os.path.join(_config_base(), "bothscreen", "config.json")


def legacy_config_path():
    """Dónde guardaba sus ajustes la versión que se llamaba «Segunda Pantalla».

    Se lee una sola vez, si todavía no hay configuración nueva, para que el
    cambio de nombre no borre las preferencias. El directorio viejo no se toca:
    lo puedes borrar a mano cuando quieras.
    """
    return os.path.join(_config_base(), "segunda-pantalla", "config.json")


def load(cfg):
    """Aplica sobre `cfg` lo que hubiera guardado. Nunca lanza."""
    path = config_path()
    if not os.path.exists(path) and os.path.exists(legacy_config_path()):
        path = legacy_config_path()
        log.info("recuperando los ajustes de la versión anterior")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as exc:
        log.warning("no se pudo leer %s (%s); se usan los valores por defecto",
                    path, exc)
        return cfg

    if not isinstance(data, dict):
        return cfg
    for key in GUARDABLES:
        if key in data and isinstance(data[key], type(getattr(cfg, key))):
            setattr(cfg, key, data[key])
    log.debug("ajustes cargados de %s", path)
    return cfg


def save(cfg):
    """Escribe de forma atómica. Un fallo aquí nunca debe romper la sesión."""
    path = config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({key: getattr(cfg, key) for key in GUARDABLES},
                      fh, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("no se pudieron guardar los ajustes: %s", exc)
