"""Vérification des mises à jour depuis GitHub Releases (lecture seule).

L'application interroge l'API publique de GitHub au plus une fois toutes les 30 minutes. En cas
d'absence de réseau, l'erreur est renvoyée à l'interface mais ne bloque jamais le reste de l'outil.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from . import config

API_URL = "https://api.github.com/repos/oliv2776/Upscale/releases/latest"
CACHE_SECONDS = 30 * 60
_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "value": None}


def parse_version(text: Any) -> tuple[int, int, int] | None:
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(text or "").strip())
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def _empty(error: str | None = None) -> dict[str, Any]:
    return {"current": config.VERSION, "latest": None, "update_available": False,
            "url": "https://github.com/oliv2776/Upscale/releases", "notes": "",
            "checked_at": datetime.now(UTC).isoformat(), "error": error}


def _query() -> dict[str, Any]:
    if not config.UPDATE_CHECK:
        return _empty("Vérification désactivée (UPSCALE_UPDATE_CHECK=0)")
    request = urllib.request.Request(
        API_URL, headers={"User-Agent": f"Upscale/{config.VERSION}",
                          "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read(1_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _empty("Aucune release publiée pour le moment")
        return _empty(f"GitHub a répondu {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _empty(f"GitHub injoignable ({getattr(exc, 'reason', exc)})")
    except ValueError:
        return _empty("Réponse GitHub illisible")
    if not isinstance(payload, dict):
        return _empty("Réponse GitHub illisible")
    latest = str(payload.get("tag_name") or "").lstrip("v")
    current, target = parse_version(config.VERSION), parse_version(latest)
    return {
        "current": config.VERSION,
        "latest": latest or None,
        "update_available": bool(current and target and target > current),
        "url": payload.get("html_url") or "https://github.com/oliv2776/Upscale/releases",
        "notes": str(payload.get("body") or "")[:8000],
        "checked_at": datetime.now(UTC).isoformat(),
        "error": None,
    }


def state(force: bool = False) -> dict[str, Any]:
    """Dernier état connu ; ``force`` ignore le cache (bouton « Vérifier »)."""
    now = time.monotonic()
    with _lock:
        cached = _cache["value"]
        if not force and cached is not None and now - float(_cache["at"]) < CACHE_SECONDS:
            return dict(cached)
    value = _query()
    with _lock:
        _cache.update(value=value, at=time.monotonic())
    return dict(value)


def reset_cache() -> None:
    with _lock:
        _cache.update(value=None, at=0.0)
