"""Réglages : valeurs par défaut, validation, profils par dossier."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import config, db

MODEL_SCALES: dict[str, tuple[int, ...]] = {
    "realesrgan-plus": (4,),
    "realesrgan-plus-anime": (4,),
    "realesr-animevideov3": (2, 3, 4),
}
# Modèles retirés (absents de Video2X 6.4) : remplacés à la lecture des réglages et des tâches.
RETIRED_MODELS = {"realesr-generalv3": "realesrgan-plus"}
MODEL_LABELS = {
    "realesrgan-plus": "Real-ESRGAN Plus",
    "realesr-animevideov3": "AnimeVideo v3",
    "realesrgan-plus-anime": "Plus Anime",
}
AUTO_MODEL = "auto"
LIVE_ACTION_MODEL = "realesrgan-plus"
ANIMATION_MODEL = "realesr-animevideov3"

PRESETS = ("ultrafast", "fast", "medium", "slow", "slower")
DEINTERLACE_MODES = ("auto", "on", "off")
DENOISE_LEVELS = ("off", "light", "medium", "strong")
CODECS = ("h264", "hevc", "av1", "h264_vaapi", "hevc_vaapi")
NOTIFY_TYPES = ("", "ntfy", "gotify", "webhook")
MEDIA_TYPES = ("", "jellyfin", "plex")
SECRET_KEYS = ("notify_token", "media_token")
PROFILE_KEYS = ("model", "output_height", "crf", "denoise", "codec", "interpolate")

DEFAULT_SETTINGS: dict[str, Any] = {
    "source": config.DEFAULT_SOURCE,
    "destination": config.DEFAULT_DESTINATION,
    "max_input_height": 480,
    "output_height": 720,
    "model": "realesrgan-plus",
    "crf": 16,
    "preset": "slow",
    "codec": "hevc",
    "include_subfolders": True,
    "deinterlace": "auto",
    "denoise": "off",
    "interpolate": 1,
    "output_suffix": "",
    "replace_original": False,
    "trash_days": 30,
    "profiles": [],
    "watch_enabled": False,
    "watch_interval": 30,
    "schedule_enabled": False,
    "schedule_start": "22:00",
    "schedule_end": "07:00",
    "notify_type": "",
    "notify_url": "",
    "notify_token": "",
    "notify_on_complete": True,
    "notify_on_failure": True,
    "media_type": "",
    "media_url": "",
    "media_token": "",
    "ollama_enabled": False,
    "ollama_url": "http://host.docker.internal:11434",
    "ollama_model": "",
    "settings_version": 2,
}
SETTINGS_VERSION = 2
TRASH_DIR = ".upscale-corbeille"

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def normalize_model(model: Any) -> Any:
    return RETIRED_MODELS.get(model, model)


def normalize(settings: dict[str, Any]) -> dict[str, Any]:
    settings["model"] = normalize_model(settings.get("model"))
    for profile in settings.get("profiles") or []:
        if "model" in profile:
            profile["model"] = normalize_model(profile["model"])
    return settings


def get_settings() -> dict[str, Any]:
    result = dict(DEFAULT_SETTINGS)
    result.update(db.load_settings_payload())
    return normalize(result)


def migrate() -> None:
    """Migrations des réglages enregistrés (appelée au démarrage)."""
    payload = db.load_settings_payload()
    if not payload or int(payload.get("settings_version", 1)) >= SETTINGS_VERSION:
        return
    # 2.2 : HEVC devient l'encodeur par défaut ; l'ancien défaut H.264 est converti.
    if payload.get("codec", "h264") == "h264":
        payload["codec"] = "hevc"
    payload["settings_version"] = SETTINGS_VERSION
    db.store_settings_payload(payload)


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Réglages renvoyés au navigateur : les jetons ne sont jamais réexposés."""
    result = dict(settings)
    for key in SECRET_KEYS:
        result[f"{key}_set"] = bool(settings.get(key))
        result[key] = ""
    return result


def safe_path(raw: str, must_exist: bool = False) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Les chemins doivent être absolus")
    try:
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError:
        raise ValueError(f"Chemin introuvable : {raw}") from None
    if resolved != config.DATA_ROOT and config.DATA_ROOT not in resolved.parents:
        raise ValueError(f"Chemin hors de la racine autorisée : {config.DATA_ROOT}")
    return resolved


def validate_folders(settings: dict[str, Any]) -> tuple[Path, Path]:
    source = safe_path(settings["source"], must_exist=True)
    dest = safe_path(settings["destination"], must_exist=False)
    if not source.is_dir():
        raise ValueError("Le dossier source doit exister et être un dossier")
    if dest == source or source in dest.parents:
        raise ValueError("La destination ne peut pas être dans le dossier source")
    if dest in source.parents:
        raise ValueError("Le dossier source ne peut pas être dans la destination")
    if dest == config.DATA_ROOT or config.DATA_ROOT not in dest.parents:
        raise ValueError(f"La destination doit rester sous {config.DATA_ROOT}")
    return source, dest


def _int_in(value: Any, key: str, low: int, high: int) -> int:
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f"{key} doit être compris entre {low} et {high}")
    return number


def _choice(value: Any, key: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise ValueError(f"Valeur invalide pour {key}")
    return value


def _url(value: Any, key: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        raise ValueError(f"{key} doit commencer par http:// ou https://")
    if len(url) > 500:
        raise ValueError(f"{key} trop longue")
    return url


def validate_model(value: Any) -> str:
    value = normalize_model(value)
    if value != AUTO_MODEL and value not in MODEL_SCALES:
        raise ValueError("Modèle non pris en charge")
    return value


def validate_profile(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw.get("path"):
        raise ValueError("Chaque profil doit indiquer un dossier")
    profile: dict[str, Any] = {"path": str(safe_path(str(raw["path"])))}
    for key in PROFILE_KEYS:
        value = raw.get(key)
        if value in (None, ""):
            continue
        profile[key] = validate_value(key, value)
    return profile


VALIDATORS: dict[str, Any] = {
    "source": str,
    "destination": str,
    "max_input_height": lambda v: _int_in(v, "max_input_height", 120, 2160),
    "output_height": lambda v: _int_in(v, "output_height", 240, 2160),
    "crf": lambda v: _int_in(v, "crf", 12, 30),
    "model": validate_model,
    "preset": lambda v: _choice(v, "preset", PRESETS),
    "codec": lambda v: _choice(v, "codec", CODECS),
    "include_subfolders": bool,
    "replace_original": bool,
    "trash_days": lambda v: _int_in(v, "trash_days", 0, 365),
    "deinterlace": lambda v: _choice(v, "deinterlace", DEINTERLACE_MODES),
    "denoise": lambda v: _choice(v, "denoise", DENOISE_LEVELS),
    "interpolate": lambda v: _int_in(v, "interpolate", 1, 2),
    "watch_enabled": bool,
    "watch_interval": lambda v: _int_in(v, "watch_interval", 5, 1440),
    "schedule_enabled": bool,
    "notify_type": lambda v: _choice(v, "notify_type", NOTIFY_TYPES),
    "notify_url": lambda v: _url(v, "L'adresse de notification"),
    "notify_on_complete": bool,
    "notify_on_failure": bool,
    "media_type": lambda v: _choice(v, "media_type", MEDIA_TYPES),
    "media_url": lambda v: _url(v, "L'adresse du serveur multimédia"),
    "ollama_enabled": bool,
    "ollama_url": lambda v: _url(v, "L'adresse Ollama"),
    "ollama_model": lambda v: str(v)[:200],
}


def validate_value(key: str, value: Any) -> Any:
    return VALIDATORS[key](value)


def _validate_time(value: Any) -> str:
    text = str(value).strip()
    if not TIME_RE.match(text):
        raise ValueError("Heure attendue au format HH:MM")
    return text


def _validate_suffix(value: Any) -> str:
    text = str(value or "")
    if len(text) > 60 or re.search(r'[/\\:*?"<>|\x00-\x1f]', text):
        raise ValueError("Suffixe de fichier invalide (60 caractères max., sans / \\ : * ? \" < > |)")
    return text


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Valide et enregistre les clés présentes. Les dossiers ne sont vérifiés que s'ils sont envoyés."""
    settings = get_settings()
    for key, value in payload.items():
        if key in VALIDATORS:
            settings[key] = validate_value(key, value)
    for key in ("schedule_start", "schedule_end"):
        if key in payload:
            settings[key] = _validate_time(payload[key])
    if "output_suffix" in payload:
        settings["output_suffix"] = _validate_suffix(payload["output_suffix"])
    if "profiles" in payload:
        profiles = payload["profiles"] or []
        if not isinstance(profiles, list) or len(profiles) > 50:
            raise ValueError("Liste de profils invalide")
        settings["profiles"] = [validate_profile(p) for p in profiles]
    for key in SECRET_KEYS:
        # Champ vide = jeton inchangé ; <clé>_clear = effacer.
        if payload.get(f"{key}_clear"):
            settings[key] = ""
        elif payload.get(key):
            settings[key] = str(payload[key]).strip()[:500]
    if "source" in payload or "destination" in payload:
        validate_folders(settings)
    db.store_settings_payload(settings)
    return settings


def matching_profile(settings: dict[str, Any], source: Path) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for profile in settings.get("profiles", []):
        folder = Path(profile["path"])
        if folder == source or folder in source.parents:
            if best is None or len(folder.parts) > len(Path(best["path"]).parts):
                best = profile
    return best


def effective_settings(settings: dict[str, Any], source: Path) -> dict[str, Any]:
    """Réglages appliqués à un fichier : réglages globaux + profil du dossier le plus précis.

    Ce résultat est figé dans la tâche au moment de la mise en file.
    """
    result = {key: value for key, value in settings.items()
              if key not in SECRET_KEYS and key != "profiles" and not key.startswith(("notify_", "media_", "watch_"))}
    profile = matching_profile(settings, source)
    if profile:
        result.update({key: value for key, value in profile.items() if key != "path"})
        result["profile"] = profile["path"]
    return result
