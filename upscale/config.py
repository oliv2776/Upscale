"""Configuration lue dans l'environnement.

Les autres modules accèdent toujours aux valeurs via ``config.NOM`` (et non ``from config import``)
pour que les tests puissent les remplacer.
"""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    """Lit UPSCALE_X, ou l'ancien nom USCALE_X (versions précédentes) s'il est seul défini."""
    value = os.environ.get(name)
    if value is None and name.startswith("UPSCALE_"):
        value = os.environ.get("USCALE_" + name[len("UPSCALE_"):])
    return (value if value is not None else default).strip()


def _int_or_none(raw: str) -> int | None:
    return int(raw) if raw.isdigit() else None


APP_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_DIR / "static"
try:
    VERSION = (APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
except OSError:
    VERSION = "dev"

STATE_DIR = Path(_env("UPSCALE_STATE_DIR", "/app/state"))
# Chemin de la bibliothèque vu depuis le conteneur Upscale.
DATA_ROOT = Path(_env("UPSCALE_DATA_ROOT", "/data")).resolve()
# Même dossier vu depuis l'hôte (mode docker : le daemon ne voit que les chemins de l'hôte).
HOST_DATA = _env("UPSCALE_HOST_DATA") or str(DATA_ROOT)
# Disques supplémentaires : chacun est monté dans /disques/<nom> (voir bibliotheques.sh).
DISKS_ROOT = Path(_env("UPSCALE_DISKS_ROOT", "/disques"))
PORT = int(_env("UPSCALE_PORT", "27000"))

# "docker" : Video2X tourne dans un conteneur frère via le socket Docker.
# "local"  : le binaire video2x est installé dans l'image Upscale (pas de socket).
RUNNER = _env("UPSCALE_RUNNER", "docker").lower()
VIDEO2X_IMAGE = _env("VIDEO2X_IMAGE", "ghcr.io/k4yt3x/video2x:6.4.0")
VIDEO2X_BIN = _env("VIDEO2X_BIN", "video2x")
# Dossier courant de Video2X en mode local (il y cherche models/...). Vide = inchangé.
VIDEO2X_WORKDIR = _env("VIDEO2X_WORKDIR")
VIDEO2X_GPU = _env("VIDEO2X_GPU")
# Moteur des modèles personnalisés (realesrgan-ncnn-vulkan, installé dans l'image).
NCNN_BIN = _env("UPSCALE_NCNN_BIN", "/opt/realesrgan/realesrgan-ncnn-vulkan")
# Convertisseur des modèles PyTorch/ONNX en ncnn (torch + spandrel + pnnx). Installé à la première conversion
# dans le dossier d'état (environ 1 Go), sauf si UPSCALE_CONVERTER_PYTHON désigne un interpréteur déjà prêt.
CONVERTER_PYTHON = _env("UPSCALE_CONVERTER_PYTHON")
TORCH_INDEX = _env("UPSCALE_TORCH_INDEX", "https://download.pytorch.org/whl/cpu")
OPENMODELDB_DATA = _env("UPSCALE_OPENMODELDB_DATA",
                        "https://raw.githubusercontent.com/OpenModelDB/open-model-database/main/data/models")
MODEL_MAX_BYTES = int(float(_env("UPSCALE_MODEL_MAX_MB", "2048")) * 1024**2)
# Segments plus courts avec ce moteur : il travaille sur des images PNG, plus volumineuses.
CUSTOM_SEGMENT_SECONDS = int(_env("UPSCALE_CUSTOM_SEGMENT_SECONDS", "30"))
VIDEO_GID = _env("VIDEO_GID", "44")
RENDER_GID = _env("RENDER_GID", "109")
VAAPI_DEVICE = _env("VAAPI_DEVICE", "/dev/dri/renderD128")

DEFAULT_SOURCE = _env("DEFAULT_SOURCE", "/data/videos")
DEFAULT_DESTINATION = _env("DEFAULT_DESTINATION", "/data/videos_upscaled")

AUTH_USER = _env("UPSCALE_USER", "admin")
AUTH_PASSWORD = os.environ.get("UPSCALE_PASSWORD", os.environ.get("USCALE_PASSWORD", ""))
TRUSTED_HOSTS = {h.strip() for h in _env("UPSCALE_TRUSTED_HOSTS").split(",") if h.strip()}

MIN_FREE_BYTES = int(float(_env("UPSCALE_MIN_FREE_GB", "5")) * 1024**3)
# Octets par pixel et par image estimés pour un intermédiaire FFV1 (sans perte).
FFV1_BYTES_PER_PIXEL = float(_env("UPSCALE_FFV1_BYTES_PER_PIXEL", "0.75"))
# Durée des segments traités séparément (reprise après coupure, disque borné).
SEGMENT_SECONDS = int(_env("UPSCALE_SEGMENT_SECONDS", "180"))

# Propriétaire des fichiers produits (utilisateur du NAS). Vide = inchangé.
PUID = _int_or_none(_env("PUID"))
PGID = _int_or_none(_env("PGID"))

LOG_RETENTION_DAYS = int(_env("UPSCALE_LOG_RETENTION_DAYS", "90"))
PREVIEW_KEEP = int(_env("UPSCALE_PREVIEW_KEEP", "10"))
SYSFS_DRM = Path(_env("UPSCALE_SYSFS_DRM", "/sys/class/drm"))

CONTAINER_LABEL = "upscale.managed=1"
# Étiquette des conteneurs créés par les versions précédentes (nom « Uscale »).
LEGACY_CONTAINER_LABEL = "uscale.managed=1"


def db_path() -> Path:
    return STATE_DIR / "upscale.sqlite3"


def log_dir() -> Path:
    return STATE_DIR / "logs"


def preview_dir() -> Path:
    return STATE_DIR / "previews"


def converter_dir() -> Path:
    return STATE_DIR / "convertisseur"


def roots() -> list[Path]:
    """Racines visibles dans Upscale : /data puis chaque disque monté dans /disques/<nom>."""
    result = [DATA_ROOT]
    try:
        result += sorted(p.resolve() for p in DISKS_ROOT.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        pass
    return result


def root_of(path: Path) -> Path | None:
    """Racine (disque) qui contient ce chemin, ou None."""
    for root in roots():
        if path == root or root in path.parents:
            return root
    return None
