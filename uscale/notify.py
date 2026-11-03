"""Notifications (ntfy, Gotify, webhook) et actualisation de Jellyfin / Plex."""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("uscale")


def _request(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None,
             method: str = "POST", timeout: float = 10) -> bytes:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read(1_000_000)


def send(settings: dict[str, Any], event: str, title: str, message: str, extra: dict[str, Any] | None = None) -> None:
    """Envoie une notification ; lève une exception en cas d'échec."""
    kind = settings.get("notify_type", "")
    url = settings.get("notify_url", "")
    token = settings.get("notify_token", "")
    if not kind or not url:
        raise ValueError("Aucun service de notification configuré")
    if kind == "ntfy":
        # Titre et étiquettes en paramètres d'URL : les en-têtes HTTP ne supportent pas les accents.
        params = urllib.parse.urlencode({"title": title, "tags": "warning" if event == "failed" else "white_check_mark",
                                         "priority": "high" if event == "failed" else "default"})
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        _request(f"{url}{'&' if '?' in url else '?'}{params}", data=message.encode("utf-8"), headers=headers)
    elif kind == "gotify":
        body = json.dumps({"title": title, "message": message, "priority": 8 if event == "failed" else 5})
        _request(url.rstrip("/") + "/message", data=body.encode("utf-8"),
                 headers={"Content-Type": "application/json", "X-Gotify-Key": token})
    else:
        body = json.dumps({"event": event, "title": title, "message": message, **(extra or {})})
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        _request(url, data=body.encode("utf-8"), headers=headers)


def notify_job(settings: dict[str, Any], event: str, job: dict[str, Any], detail: str = "") -> None:
    """Notification asynchrone en fin de tâche, selon les préférences."""
    if not settings.get("notify_type") or not settings.get("notify_url"):
        return
    if event == "completed" and not settings.get("notify_on_complete", True):
        return
    if event == "failed" and not settings.get("notify_on_failure", True):
        return
    name = str(job.get("source", "")).rsplit("/", 1)[-1]
    title = "Uscale : vidéo terminée" if event == "completed" else "Uscale : échec du traitement"
    message = f"{name}\n{detail}".strip()

    def run() -> None:
        try:
            send(settings, event, title, message, {"job_id": job.get("id"), "source": job.get("source"),
                                                    "destination": job.get("destination")})
        except Exception as exc:  # noqa: BLE001
            log.warning("Notification impossible : %s", exc)

    threading.Thread(target=run, daemon=True).start()


def refresh_media_server(settings: dict[str, Any]) -> str:
    """Demande à Jellyfin ou Plex de réanalyser ses bibliothèques. Renvoie un message."""
    kind = settings.get("media_type", "")
    base = str(settings.get("media_url", "")).rstrip("/")
    token = settings.get("media_token", "")
    if not kind or not base:
        raise ValueError("Aucun serveur multimédia configuré")
    if kind == "jellyfin":
        _request(base + "/Library/Refresh", data=b"", headers={"X-Emby-Token": token})
        return "Jellyfin : actualisation de la bibliothèque demandée"
    query = urllib.parse.urlencode({"X-Plex-Token": token})
    raw = _request(f"{base}/library/sections?{query}", method="GET", headers={"Accept": "application/json"})
    sections = json.loads(raw.decode("utf-8")).get("MediaContainer", {}).get("Directory", [])
    for section in sections:
        _request(f"{base}/library/sections/{section['key']}/refresh?{query}", method="GET")
    return f"Plex : {len(sections)} bibliothèque(s) en cours d'actualisation"


def refresh_async(settings: dict[str, Any]) -> None:
    if not settings.get("media_type") or not settings.get("media_url"):
        return

    def run() -> None:
        try:
            refresh_media_server(settings)
        except Exception as exc:  # noqa: BLE001
            log.warning("Actualisation du serveur multimédia impossible : %s", exc)

    threading.Thread(target=run, daemon=True).start()
