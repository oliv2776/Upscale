"""Client Ollama : état de l'instance et choix automatique du modèle par un modèle de vision."""
from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import media
from .settings import ANIMATION_MODEL, LIVE_ACTION_MODEL

CLASSIFY_PROMPT = (
    "These are frames from one video. Is it animation (cartoon, anime, drawn or CGI animated) "
    "or live action (filmed real people or places)? Answer with exactly one word: ANIMATION or LIVE."
)


def get_json(base: str, path: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(base + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read(1_000_000).decode("utf-8"))


def post_json(base: str, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read(1_000_000).decode("utf-8"))


def candidates(settings: dict[str, Any]) -> list[str]:
    result = []
    configured = str(settings.get("ollama_url", "")).strip().rstrip("/")
    if configured:
        result.append(configured)
    for candidate in ("http://host.docker.internal:11434", "http://172.17.0.1:11434"):
        if candidate not in result:
            result.append(candidate)
    return result


def status(settings: dict[str, Any]) -> dict[str, Any]:
    errors = []
    for base in candidates(settings):
        try:
            payload = get_json(base, "/api/tags", 3)
            models = [item.get("name") for item in payload.get("models", []) if item.get("name")]
            try:
                version = get_json(base, "/api/version", 2).get("version", "inconnue")
            except (OSError, ValueError, urllib.error.URLError):
                version = "inconnue"
            return {"enabled": bool(settings.get("ollama_enabled")), "connected": True, "url": base,
                    "version": version, "models": models, "selected_model": settings.get("ollama_model", "")}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{base}: {str(exc)[:160]}")
    return {"enabled": bool(settings.get("ollama_enabled")), "connected": False,
            "url": settings.get("ollama_url", ""), "models": [],
            "message": "Ollama non joignable depuis le conteneur Upscale", "details": errors}


def extract_frames(source: Path, duration: float, folder: Path) -> list[Path]:
    frames = []
    for index, ratio in enumerate((0.2, 0.5, 0.8)):
        output = folder / f"frame{index}.jpg"
        at = max(0.0, duration * ratio) if duration > 0 else index * 5.0
        subprocess.run(media.frame_extract_command(source, output, at), capture_output=True, timeout=60)
        if output.exists() and output.stat().st_size:
            frames.append(output)
    return frames


def classify(settings: dict[str, Any], source: Path, duration: float) -> tuple[str, str]:
    """Renvoie (modèle Real-ESRGAN, explication). Repli sur le modèle « prise de vue réelle » en cas d'échec."""
    base = str(settings.get("ollama_url", "")).strip().rstrip("/")
    model = settings.get("ollama_model", "")
    if not settings.get("ollama_enabled") or not base or not model:
        return LIVE_ACTION_MODEL, "Ollama désactivé ou sans modèle : modèle général utilisé"
    with tempfile.TemporaryDirectory() as tmp:
        frames = extract_frames(source, duration, Path(tmp))
        if not frames:
            return LIVE_ACTION_MODEL, "Impossible d'extraire des images : modèle général utilisé"
        images = [base64.b64encode(frame.read_bytes()).decode("ascii") for frame in frames]
    try:
        answer = post_json(base, "/api/generate", {"model": model, "prompt": CLASSIFY_PROMPT, "images": images,
                                                   "stream": False, "options": {"temperature": 0}}, 180)
    except Exception as exc:  # noqa: BLE001
        return LIVE_ACTION_MODEL, f"Ollama n'a pas répondu ({str(exc)[:120]}) : modèle général utilisé"
    text = str(answer.get("response", "")).strip().upper()
    if "ANIM" in text:
        return ANIMATION_MODEL, f"Ollama ({model}) : animation détectée"
    return LIVE_ACTION_MODEL, f"Ollama ({model}) : prise de vue réelle détectée"
