"""Configuration lue dans l'environnement.

Les autres modules accèdent toujours aux valeurs via ``config.NOM`` (et non ``from config import``)
pour que les tests puissent les remplacer.
"""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int_or_none(raw: str) -> int | None:
    return int(raw) if raw.isdigit() else None


APP_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_DIR / "static"
try:
    VERSION = (APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
except OSError:
    VERSION = "dev"

STATE_DIR = Path(_env("USCALE_STATE_DIR", "/app/state"))
# Chemin de la bibliothèque vu depuis le conteneur Uscale.
DATA_ROOT = Path(_env("USCALE_DATA_ROOT", "/data")).resolve()
# Même dossier vu depuis l'hôte (mode docker : le daemon ne voit que les chemins de l'hôte).
HOST_DATA = _env("USCALE_HOST_DATA") or str(DATA_ROOT)
PORT = int(_env("USCALE_PORT", "27000"))

# "docker" : Video2X tourne dans un conteneur frère via le socket Docker.
# "local"  : le binaire video2x est installé dans l'image Uscale (pas de socket).
RUNNER = _env("USCALE_RUNNER", "docker").lower()
VIDEO2X_IMAGE = _env("VIDEO2X_IMAGE", "ghcr.io/k4yt3x/video2x:6.4.0")
VIDEO2X_BIN = _env("VIDEO2X_BIN", "video2x")
# Dossier courant de Video2X en mode local (il y cherche models/...). Vide = inchangé.
VIDEO2X_WORKDIR = _env("VIDEO2X_WORKDIR")
VIDEO2X_GPU = _env("VIDEO2X_GPU")
VIDEO_GID = _env("VIDEO_GID", "44")
RENDER_GID = _env("RENDER_GID", "109")
VAAPI_DEVICE = _env("VAAPI_DEVICE", "/dev/dri/renderD128")

DEFAULT_SOURCE = _env("DEFAULT_SOURCE", "/data/videos")
DEFAULT_DESTINATION = _env("DEFAULT_DESTINATION", "/data/videos_upscaled")

AUTH_USER = _env("USCALE_USER", "admin")
AUTH_PASSWORD = os.environ.get("USCALE_PASSWORD", "")
TRUSTED_HOSTS = {h.strip() for h in _env("USCALE_TRUSTED_HOSTS").split(",") if h.strip()}

MIN_FREE_BYTES = int(float(_env("USCALE_MIN_FREE_GB", "5")) * 1024**3)
# Octets par pixel et par image estimés pour un intermédiaire FFV1 (sans perte).
FFV1_BYTES_PER_PIXEL = float(_env("USCALE_FFV1_BYTES_PER_PIXEL", "0.75"))
# Durée des segments traités séparément (reprise après coupure, disque borné).
SEGMENT_SECONDS = int(_env("USCALE_SEGMENT_SECONDS", "180"))

# Propriétaire des fichiers produits (utilisateur du NAS). Vide = inchangé.
PUID = _int_or_none(_env("PUID"))
PGID = _int_or_none(_env("PGID"))

LOG_RETENTION_DAYS = int(_env("USCALE_LOG_RETENTION_DAYS", "90"))
PREVIEW_KEEP = int(_env("USCALE_PREVIEW_KEEP", "10"))
SYSFS_DRM = Path(_env("USCALE_SYSFS_DRM", "/sys/class/drm"))

CONTAINER_LABEL = "uscale.managed=1"


def db_path() -> Path:
    return STATE_DIR / "uscale.sqlite3"


def log_dir() -> Path:
    return STATE_DIR / "logs"


def preview_dir() -> Path:
    return STATE_DIR / "previews"
