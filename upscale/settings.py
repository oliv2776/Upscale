"""Réglages : valeurs par défaut, validation, profils par dossier."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import config, db, models

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
# Rendu : part de l'IA dans l'image finale et grain ajouté (contre l'effet « peau cireuse »).
RENDERS = ("ai", "peau-normale", "custom")
RENDER_PRESETS = {"ai": (100, "off"), "peau-normale": (70, "light")}
RENDER_LABELS = {"ai": "IA pure", "peau-normale": "Peau normale", "custom": "Personnalisé"}
GRAIN_LEVELS = ("off", "light", "medium")
CODECS = ("h264", "hevc", "av1", "h264_vaapi", "hevc_vaapi")
NOTIFY_TYPES = ("", "ntfy", "gotify", "webhook")
MEDIA_TYPES = ("", "jellyfin", "plex", "emby")
SECRET_KEYS = ("notify_token", "media_token")
PROFILE_KEYS = ("model", "output_height", "crf", "denoise", "codec", "interpolate", "render")

DEFAULT_SETTINGS: dict[str, Any] = {
    "source": config.DEFAULT_SOURCE,
    "sources": [config.DEFAULT_SOURCE],
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
    "render": "ai",
    "ai_strength": 70,
    "grain": "light",
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
    settings["sources"] = source_list(settings)
    if settings["sources"]:
        settings["source"] = settings["sources"][0]
    for profile in settings.get("profiles") or []:
        if "model" in profile:
            profile["model"] = normalize_model(profile["model"])
    return settings


def get_settings() -> dict[str, Any]:
    payload = db.load_settings_payload()
    result = dict(DEFAULT_SETTINGS)
    result.update(payload)
    if "sources" not in payload and payload.get("source"):
        result["sources"] = [payload["source"]]  # réglages d'avant 2.3 : un seul dossier source
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
    if config.root_of(resolved) is None:
        allowed = ", ".join(str(r) for r in config.roots())
        raise ValueError(f"Chemin hors des dossiers accessibles ({allowed})")
    return resolved


def source_list(settings: dict[str, Any]) -> list[str]:
    """Dossiers source (liste) ; les réglages d'avant 2.3 n'ont qu'un champ « source »."""
    sources = [str(s) for s in settings.get("sources") or [] if str(s).strip()]
    return sources or ([str(settings["source"])] if settings.get("source") else [])


def validate_folders(settings: dict[str, Any]) -> tuple[list[Path], Path]:
    sources = [safe_path(raw, must_exist=True) for raw in source_list(settings)]
    if not sources:
        raise ValueError("Ajoute au moins un dossier source")
    dest = safe_path(settings["destination"], must_exist=False)
    for source in sources:
        if not source.is_dir():
            raise ValueError(f"Le dossier source doit exister et être un dossier : {source}")
        if dest == source:
            raise ValueError("La destination ne peut pas être le dossier source lui-même")
        if dest in source.parents:
            raise ValueError(f"Le dossier source ne peut pas être dans la destination : {source}")
    # Dossiers imbriqués : le plus large suffit (les profils gèrent les cas particuliers).
    sources = sorted(set(sources), key=lambda path: (len(path.parts), str(path)))
    unique: list[Path] = []
    for source in sources:
        if any(parent == source or parent in source.parents for parent in unique):
            continue
        unique.append(source)
    if config.root_of(dest) in (None, dest):
        raise ValueError("La destination doit être un sous-dossier d'un disque (ex. /data/videos_upscaled)")
    return unique, dest


def source_root_for(path: Path, settings: dict[str, Any]) -> Path:
    """Dossier source qui contient ce fichier (le plus précis), ou son dossier parent."""
    best = None
    for raw in source_list(settings):
        root = Path(raw)
        if root == path or root in path.parents:
            if best is None or len(root.parts) > len(best.parts):
                best = root
    return best or path.parent


def _int_in(value: Any, key: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{key} doit être un nombre entier")
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f"{key} doit être compris entre {low} et {high}")
    return number


TRUE_VALUES = {"1", "true", "yes", "on", "oui", "vrai"}
FALSE_VALUES = {"0", "false", "no", "off", "non", "faux"}


def _bool(value: Any) -> bool:
    """Booléen strict : ``bool("false")`` vaut ``True`` en Python, ce qui activerait des options dangereuses."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ValueError("Valeur booléenne attendue (true/false)")


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
    if value == AUTO_MODEL or value in MODEL_SCALES or (models.is_custom(value) and models.get(value)):
        return value
    raise ValueError("Modèle non pris en charge (ou modèle personnalisé supprimé)")


def model_scales(model: str) -> tuple[int, ...]:
    """Facteurs disponibles : ceux de Video2X, ou le facteur unique d'un modèle personnalisé."""
    if models.is_custom(model):
        meta = models.get(model)
        return (int(meta["scale"]),) if meta else (4,)
    return MODEL_SCALES.get(model, (4,))


def model_label(model: str) -> str:
    if models.is_custom(model):
        meta = models.get(model)
        return f"{meta['label']} (personnalisé)" if meta else f"{model} (supprimé)"
    return MODEL_LABELS.get(model, model)


def available_models() -> dict[str, dict[str, Any]]:
    """Modèles proposés dans l'interface : intégrés puis personnalisés."""
    result: dict[str, dict[str, Any]] = {
        key: {"label": MODEL_LABELS[key], "scales": list(scales), "custom": False}
        for key, scales in MODEL_SCALES.items()}
    for meta in models.list_models():
        result[meta["id"]] = {"label": meta["label"], "scales": [meta["scale"]], "custom": True,
                              "tile": meta["tile"], "param_size": meta["param_size"], "bin_size": meta["bin_size"],
                              **{k: meta.get(k) for k in ("architecture", "license", "author", "page", "verified")
                                 if meta.get(k)}}
    return result


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
    "include_subfolders": _bool,
    "replace_original": _bool,
    "trash_days": lambda v: _int_in(v, "trash_days", 0, 365),
    "deinterlace": lambda v: _choice(v, "deinterlace", DEINTERLACE_MODES),
    "denoise": lambda v: _choice(v, "denoise", DENOISE_LEVELS),
    "interpolate": lambda v: _int_in(v, "interpolate", 1, 2),
    "render": lambda v: _choice(v, "render", RENDERS),
    "ai_strength": lambda v: _int_in(v, "ai_strength", 30, 100),
    "grain": lambda v: _choice(v, "grain", GRAIN_LEVELS),
    "watch_enabled": _bool,
    "watch_interval": lambda v: _int_in(v, "watch_interval", 5, 1440),
    "schedule_enabled": _bool,
    "notify_type": lambda v: _choice(v, "notify_type", NOTIFY_TYPES),
    "notify_url": lambda v: _url(v, "L'adresse de notification"),
    "notify_on_complete": _bool,
    "notify_on_failure": _bool,
    "media_type": lambda v: _choice(v, "media_type", MEDIA_TYPES),
    "media_url": lambda v: _url(v, "L'adresse du serveur multimédia"),
    "ollama_enabled": _bool,
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
    if "sources" in payload:
        sources = payload["sources"] or []
        if not isinstance(sources, list) or len(sources) > 30:
            raise ValueError("Liste de dossiers source invalide")
        settings["sources"] = [str(s).strip() for s in sources if str(s).strip()]
        settings["source"] = settings["sources"][0] if settings["sources"] else ""
    elif "source" in payload:
        settings["sources"] = [str(payload["source"])]
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
    if "source" in payload or "sources" in payload or "destination" in payload:
        sources, destination = validate_folders(settings)
        settings["sources"] = [str(source) for source in sources]
        settings["source"] = settings["sources"][0] if settings["sources"] else ""
        settings["destination"] = str(destination)
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


def render_params(settings: dict[str, Any]) -> tuple[int, str]:
    """(part de l'IA en %, grain) pour le rendu choisi."""
    render = settings.get("render", "ai")
    if render in RENDER_PRESETS:
        return RENDER_PRESETS[render]
    return int(settings.get("ai_strength", 70)), str(settings.get("grain", "off"))
