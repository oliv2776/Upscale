"""Interface web et API HTTP."""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from . import config, converter, db, gpu, media, models, notify, ollama, runner, scanner, updates, worker
from . import settings as settings_mod

app = Flask(__name__, static_folder=str(config.STATIC_DIR), static_url_path="/static")
app.json.sort_keys = False  # garder l'ordre des modèles
app.config["MAX_CONTENT_LENGTH"] = 2100 * 1024**2  # import de modèles personnalisés
UPLOAD_PATHS = {"/api/models"}
PREVIEW_FILE_RE = re.compile(r"[a-z0-9-]+\.(mp4|json)")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Réglage « réseau local » : autorise les actions sans mot de passe, mémorisé en base (réversible).
LAN_MODE_PATH = "/api/auth/lan-mode"
# Anti-bruteforce de l'authentification Basic : échecs par IP sur une fenêtre glissante.
AUTH_MAX_FAILURES = 15
AUTH_FAILURE_WINDOW = 300.0
_auth_failures: dict[str, list[float]] = {}
_auth_lock = threading.Lock()


def _auth_blocked(ip: str) -> bool:
    now = time.monotonic()
    with _auth_lock:
        recent = [moment for moment in _auth_failures.get(ip, []) if now - moment < AUTH_FAILURE_WINDOW]
        _auth_failures[ip] = recent
        return len(recent) >= AUTH_MAX_FAILURES


def _auth_failed(ip: str) -> None:
    with _auth_lock:
        _auth_failures.setdefault(ip, []).append(time.monotonic())


def _auth_ok(ip: str) -> None:
    with _auth_lock:
        _auth_failures.pop(ip, None)


_lan_lock = threading.Lock()
_lan_cache: dict[str, Any] = {"value": None, "at": 0.0}


def _lan_mode() -> bool:
    """Accès sans mot de passe : forcé par .env, ou choisi depuis l'interface (base)."""
    if config.ALLOW_ANONYMOUS:
        return True
    now = time.monotonic()
    with _lan_lock:
        cached = _lan_cache["value"]
        if cached is not None and now - float(_lan_cache["at"]) < 2.0:
            return bool(cached)
    value = db.get_state("lan_mode") == "1"
    with _lan_lock:
        _lan_cache.update(value=value, at=now)
    return value


def _set_lan_mode(value: bool) -> None:
    db.set_state("lan_mode", "1" if value else "0")
    with _lan_lock:
        _lan_cache.update(value=value, at=time.monotonic())


def _auth_state() -> dict[str, Any]:
    return {"password_set": bool(config.AUTH_PASSWORD), "anonymous_allowed": _lan_mode(),
            "env_forced": bool(config.ALLOW_ANONYMOUS)}


def _trusted_origin(origin: str) -> bool:
    """Origine autorisée : même site que l'hôte de la requête, et nom explicitement de confiance.

    On n'utilise ni ``request.host`` seul (DNS rebinding : l'attaquant contrôle Host et Origin) ni
    ``X-Forwarded-Host`` (fourni par le client). Derrière un reverse proxy, le nom public doit être
    listé dans UPSCALE_TRUSTED_HOSTS.
    """
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.netloc.lower() != request.host.lower():
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in config.TRUSTED_HOSTS or host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


@app.before_request
def guard_request() -> Response | tuple[Response, int] | None:
    if request.path == "/api/ping":
        return None
    client = request.remote_addr or "inconnu"
    if config.AUTH_PASSWORD:
        if _auth_blocked(client):
            return Response("Trop de tentatives d'authentification", 429, {"Retry-After": "300"})
        auth = request.authorization
        valid = bool(auth and auth.type == "basic"
                     and hmac.compare_digest(str(auth.username or ""), config.AUTH_USER)
                     and hmac.compare_digest(str(auth.password or ""), config.AUTH_PASSWORD))
        if not valid:
            _auth_failed(client)
            return Response("Authentification requise", 401, {"WWW-Authenticate": 'Basic realm="Upscale"'})
        _auth_ok(client)
    elif (not _lan_mode() and request.method not in SAFE_METHODS
          and request.path != LAN_MODE_PATH):
        return error("Aucun mot de passe configuré : active le mode réseau local dans l'interface "
                     "(ou définis UPSCALE_PASSWORD / UPSCALE_ALLOW_ANONYMOUS=1 dans .env).", 403)
    if request.method not in SAFE_METHODS:
        # Un JSON explicite (ou l'en-tête d'import) force un preflight CORS : un site tiers ne peut pas
        # piloter l'API.
        upload = request.path in UPLOAD_PATHS and request.headers.get("X-Upscale-Upload") == "1"
        if not request.is_json and not upload:
            return error("Content-Type application/json requis", 415)
        origin = request.headers.get("Origin")
        if origin and not _trusted_origin(origin):
            return error("Origine refusée", 403)
    return None


@app.after_request
def security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    # Après une mise à jour d'image, le navigateur doit revalider l'HTML et les assets (sinon ancienne UI en cache).
    if request.path == "/" or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("Corps JSON invalide")
    return payload


def valid_job_id(job_id: str) -> bool:
    return bool(worker.JOB_ID_RE.fullmatch(job_id))


LIBRARY_ID_RE = re.compile(r"[a-f0-9]{16}")
LIBRARY_FILTERS = {"all", "new", "queued", "done"}


def valid_library_id(item_id: str) -> bool:
    return bool(LIBRARY_ID_RE.fullmatch(item_id))


# ---------------------------------------------------------------- pages et réglages

@app.get("/")
def index():
    return send_from_directory(config.STATIC_DIR, "index.html")


@app.get("/api/ping")
def api_ping():
    return jsonify({"ok": True, "version": config.VERSION})


@app.post(LAN_MODE_PATH)
def api_lan_mode():
    """Active ou désactive les actions sans mot de passe (choix explicite, mémorisé en base)."""
    try:
        payload = json_body()
    except ValueError as exc:
        return error(str(exc))
    if config.AUTH_PASSWORD:
        return error("Un mot de passe est configuré : le mode sans mot de passe est désactivé.", 409)
    if config.ALLOW_ANONYMOUS:
        return error("UPSCALE_ALLOW_ANONYMOUS=1 est défini dans .env : l'accès est déjà autorisé.", 409)
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        return error("Valeur booléenne attendue")
    _set_lan_mode(enabled)
    return jsonify({"ok": True, "auth": _auth_state()})


@app.get("/api/config")
def api_config():
    return jsonify({
        "settings": settings_mod.public_settings(settings_mod.get_settings()),
        "data_root": str(config.DATA_ROOT), "disks": disk_list(), "image": config.VIDEO2X_IMAGE,
        "runner": config.RUNNER,
        "version": config.VERSION, "segment_seconds": config.SEGMENT_SECONDS,
        "models": settings_mod.available_models(),
        "auth": _auth_state(),
    })


@app.post("/api/config")
def api_save_config():
    try:
        saved = settings_mod.save_settings(json_body())
        return jsonify({"ok": True, "settings": settings_mod.public_settings(saved)})
    except Exception as exc:  # noqa: BLE001
        return error(str(exc))


def disk_list() -> list[dict[str, Any]]:
    result = []
    for root in config.roots():
        try:
            usage = shutil.disk_usage(root)
            free, total = usage.free, usage.total
        except OSError:
            free = total = None
        label = "Disque principal (/data)" if root == config.DATA_ROOT else root.name
        result.append({"name": label, "path": str(root), "free": free, "total": total})
    return result


@app.get("/api/browse")
def api_browse():
    try:
        raw = request.args.get("path", "")
        with_files = request.args.get("files") == "1"
        if not raw.strip():
            # Vue d'accueil : la liste des disques accessibles.
            return jsonify({"path": "", "parent": None, "disks": disk_list(), "directories": [], "files": []})
        path = settings_mod.safe_path(raw, must_exist=True)
        if path.is_file():
            path = path.parent
        if not path.is_dir():
            return jsonify({"error": "Ce chemin n'est pas un dossier"}), 400
        dirs, files = [], []
        for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            try:
                if child.is_symlink() or child.name.startswith((".upscale", ".uscale")):
                    continue
                if child.is_dir():
                    settings_mod.safe_path(str(child), must_exist=True)
                    dirs.append({"name": child.name, "path": str(child)})
                elif with_files and child.suffix.lower() in media.VIDEO_EXTENSIONS:
                    files.append({"name": child.name, "path": str(child), "size": child.stat().st_size})
            except (OSError, ValueError):
                continue
        parent = "" if path == config.root_of(path) else str(path.parent)
        return jsonify({"path": str(path), "parent": parent, "directories": dirs, "files": files})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


# ---------------------------------------------------------------- analyse

def start_scan_from_request(enqueue_files: bool):
    try:
        saved = settings_mod.save_settings(json_body())
    except Exception as exc:  # noqa: BLE001
        return error(str(exc))
    if not scanner.start_scan(saved, enqueue_files):
        return error("Une analyse est déjà en cours", 409)
    if enqueue_files:
        worker.ensure_worker()
    return jsonify({"ok": True, "started": True}), 202


@app.post("/api/scan")
def api_scan():
    return start_scan_from_request(enqueue_files=bool(request.args.get("enqueue")))


@app.post("/api/queue")
def api_queue():
    return start_scan_from_request(enqueue_files=True)


@app.get("/api/scan")
def api_scan_status():
    return jsonify(scanner.get_scan_state())


# ---------------------------------------------------------------- file

STATUS_FILTERS = {"queued", "running", "completed", "failed", "cancelled"}


@app.get("/api/jobs")
def api_jobs():
    status = request.args.get("status", "")
    kind = request.args.get("kind", "upscale")
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 300))
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        return error("Pagination invalide")
    if kind == "all":
        where, params = [], []
    else:
        where, params = ["kind=?"], [kind if kind in {"upscale", "preview"} else "upscale"]
    if status in STATUS_FILTERS:
        where.append("status=?")
        params.append(status)
    clause = " AND ".join(where) if where else "1=1"
    order = ("CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
             "CASE WHEN status='queued' THEN priority END DESC, "
             "CASE WHEN status='queued' THEN position END ASC, updated_at DESC")
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM jobs WHERE {clause}", params).fetchone()["n"]
        rows = conn.execute(f"SELECT * FROM jobs WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                            [*params, limit, (page - 1) * limit]).fetchall()
        counts = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status,COUNT(*) AS n FROM jobs WHERE kind='upscale' GROUP BY status").fetchall()}
        preview_counts = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status,COUNT(*) AS n FROM jobs WHERE kind='preview' GROUP BY status").fetchall()}
    jobs = []
    for row in rows:
        item = dict(row)
        snapshot = json.loads(item.pop("settings_json") or "{}")
        item["model"] = item.get("resolved_model") or snapshot.get("model")
        item["output_height"] = snapshot.get("output_height")
        item["codec"] = snapshot.get("codec")
        item["profile"] = snapshot.get("profile")
        item["render"] = snapshot.get("render", "ai")
        item["replace_original"] = bool(snapshot.get("replace_original"))
        jobs.append(item)
    return jsonify({"jobs": jobs, "counts": counts, "preview_counts": preview_counts,
                    "total": total, "page": page, "limit": limit,
                    "paused": db.is_paused(), "deferred": worker.defer_reason(), "active_job_id": runner.get_active()})


@app.post("/api/pause")
def api_pause():
    db.set_paused(True)
    return jsonify({"ok": True})


@app.post("/api/resume")
def api_resume():
    db.set_paused(False)
    scanner.worker_wakeup.set()
    worker.ensure_worker()
    return jsonify({"ok": True})


@app.post("/api/cancel/<job_id>")
@app.post("/api/jobs/<job_id>/cancel")
def api_cancel(job_id: str):
    if not valid_job_id(job_id):
        return error("Tâche introuvable", 404)
    try:
        if not worker.cancel(job_id):
            return error("Tâche introuvable", 404)
    except ValueError as exc:
        return error(str(exc), 409)
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/retry")
def api_retry(job_id: str):
    try:
        worker.retry(job_id)
        worker.ensure_worker()
        return jsonify({"ok": True})
    except ValueError as exc:
        return error(str(exc))


@app.post("/api/jobs/replace-originals")
def api_replace_all_originals():
    """Remplace les originaux de toutes les tâches terminées par leur sortie de destination."""
    return jsonify({"ok": True, **worker.replace_all_from_destination()})


@app.post("/api/jobs/<job_id>/replace-original")
def api_replace_original(job_id: str):
    if not valid_job_id(job_id):
        return error("Tâche introuvable", 404)
    try:
        target = worker.replace_from_destination(job_id)
        return jsonify({"ok": True, "destination": str(target)})
    except LookupError as exc:
        return error(str(exc), 404)
    except ValueError as exc:
        return error(str(exc), 409)
    except OSError as exc:
        return error(f"Déplacement impossible : {exc}", 500)


@app.post("/api/jobs/<job_id>/delete")
def api_delete(job_id: str):
    if not valid_job_id(job_id):
        return error("Identifiant invalide")
    try:
        worker.delete(job_id)
        return jsonify({"ok": True})
    except LookupError as exc:
        return error(str(exc), 404)
    except ValueError as exc:
        return error(str(exc))


@app.post("/api/jobs/<job_id>/restore")
def api_restore(job_id: str):
    if not valid_job_id(job_id):
        return error("Identifiant invalide")
    try:
        worker.restore(job_id)
        return jsonify({"ok": True})
    except LookupError as exc:
        return error(str(exc), 404)
    except (ValueError, OSError) as exc:
        return error(str(exc))


@app.post("/api/jobs/<job_id>/move")
def api_move(job_id: str):
    try:
        payload = json_body()
        where = str(payload.get("to", ""))
        if where in {"before", "after"}:
            target = str(payload.get("id", ""))
            if not valid_job_id(target):
                return error("Tâche cible invalide")
            worker.move_relative(job_id, target, where)
        elif where == "index":
            try:
                index = int(payload.get("index"))
            except (TypeError, ValueError):
                return error("Index de déplacement invalide")
            worker.move_to(job_id, index)
        else:
            worker.move(job_id, where)
        return jsonify({"ok": True})
    except ValueError as exc:
        return error(str(exc))


@app.post("/api/jobs/clear")
def api_clear():
    try:
        statuses = json_body().get("statuses", ["completed"])
        return jsonify({"ok": True, "deleted": worker.clear(list(statuses))})
    except ValueError as exc:
        return error(str(exc))


@app.get("/api/logs/<job_id>")
def api_logs(job_id: str):
    if not valid_job_id(job_id):
        return error("Identifiant invalide")
    path = config.log_dir() / f"{job_id}.log"
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 60000))
            return jsonify({"log": stream.read().decode("utf-8", errors="replace")[-30000:]})
    except OSError:
        return jsonify({"log": ""})


# ---------------------------------------------------------------- aperçus

@app.post("/api/preview")
def api_preview():
    try:
        payload = json_body()
        source = settings_mod.safe_path(str(payload.get("source", "")), must_exist=True)
        if not source.is_file() or source.suffix.lower() not in media.VIDEO_EXTENSIONS:
            return error("Choisis un fichier vidéo")
        start = max(0.0, float(payload.get("start", 0)))
        duration = float(payload.get("duration", 10))
        if not 2 <= duration <= 60:
            return error("Durée d'aperçu entre 2 et 60 s")
        available = settings_mod.available_models()
        models = [m for m in payload.get("models", []) if m in available]
        if not models:
            return error("Choisis au moins un modèle")
        job_id = worker.create_preview(source, start, duration, models, bool(payload.get("peau_normale", True)))
        return jsonify({"ok": True, "id": job_id}), 202
    except (ValueError, OSError) as exc:
        return error(str(exc))


def _thumbnail_target(source: Path, key: str) -> Path | None:
    """Extrait et met en cache une image de la vidéo ; None si impossible."""
    try:
        source_stat = source.stat()
    except OSError:
        return None
    folder = config.thumbnail_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{key}.jpg"
    try:
        fresh = target.exists() and target.stat().st_mtime >= source_stat.st_mtime
    except OSError:
        fresh = False
    if not fresh:
        temp = folder / f".{key}.{uuid.uuid4().hex[:8]}.jpg"
        try:
            seconds = media.probe_duration(source)
            at = min(max(seconds * 0.2, 0.0), 120.0) if seconds > 0 else 5.0
            result = subprocess.run(media.frame_extract_command(source, temp, at),
                                    capture_output=True, timeout=30)
            if result.returncode != 0 or not temp.exists() or temp.stat().st_size == 0:
                temp.unlink(missing_ok=True)
                return None
            temp.replace(target)
        except (OSError, subprocess.TimeoutExpired):
            temp.unlink(missing_ok=True)
            return None
    return target


def _thumbnail_response(source: Path, key: str):
    target = _thumbnail_target(source, key)
    if target is None:
        return error("Miniature indisponible", 404)
    return send_file(target, mimetype="image/jpeg", conditional=True, max_age=3600)


@app.get("/api/jobs/<job_id>/thumbnail")
def api_job_thumbnail(job_id: str):
    if not valid_job_id(job_id):
        return error("Tâche introuvable", 404)
    job = db.get_job(job_id)
    if not job:
        return error("Tâche introuvable", 404)
    return _thumbnail_response(Path(job["source"]), job_id)


@app.get("/api/library")
def api_library():
    try:
        page = max(1, int(request.args.get("page", 1)))
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except ValueError:
        return error("Pagination invalide")
    query = request.args.get("q", "").strip()[:200]
    status = request.args.get("filter", "all")
    if status not in LIBRARY_FILTERS:
        status = "all"
    try:
        min_duration = float(request.args["min_duration"]) if request.args.get("min_duration") else None
        max_duration = float(request.args["max_duration"]) if request.args.get("max_duration") else None
    except ValueError:
        return error("Durée invalide")
    if (min_duration is not None and min_duration < 0) or (max_duration is not None and max_duration < 0):
        return error("Durée invalide")
    if min_duration is not None and max_duration is not None and min_duration > max_duration:
        return error("La durée minimale dépasse la durée maximale")
    sort = request.args.get("sort", "name")
    if sort not in {"name", "duration_asc", "duration_desc", "resolution", "size"}:
        sort = "name"
    data = db.library_page(page, limit, query, status, min_duration, max_duration, sort)
    items = []
    for row in data["items"]:
        item = dict(row)
        item["thumbnail"] = f"/api/library/{item['id']}/thumbnail"
        items.append(item)
    return jsonify({**data, "items": items, "q": query, "filter": status, "sort": sort,
                    "min_duration": min_duration, "max_duration": max_duration})


@app.get("/api/library/ids")
def api_library_ids():
    query = request.args.get("q", "").strip()[:200]
    status = request.args.get("filter", "all")
    if status not in LIBRARY_FILTERS:
        status = "all"
    try:
        min_duration = float(request.args["min_duration"]) if request.args.get("min_duration") else None
        max_duration = float(request.args["max_duration"]) if request.args.get("max_duration") else None
    except ValueError:
        return error("Durée invalide")
    if (min_duration is not None and max_duration is not None and min_duration > max_duration):
        return error("La durée minimale dépasse la durée maximale")
    return jsonify({"ids": db.library_ids(query, status, min_duration, max_duration)})


@app.get("/api/library/<item_id>/thumbnail")
def api_library_thumbnail(item_id: str):
    if not valid_library_id(item_id):
        return error("Vidéo introuvable", 404)
    row = db.library_item(item_id)
    if not row:
        return error("Vidéo introuvable", 404)
    return _thumbnail_response(Path(row["source"]), f"lib-{item_id}")


@app.post("/api/library/queue")
def api_library_queue():
    try:
        ids = json_body().get("ids")
        if not isinstance(ids, list) or not ids:
            return error("Aucune vidéo sélectionnée")
        ids = list(dict.fromkeys(str(item) for item in ids if valid_library_id(str(item))))
        if not ids:
            return error("Aucune vidéo sélectionnée")
        if len(ids) > 20000:
            return error("Sélection limitée à 20 000 vidéos")
        settings = settings_mod.get_settings()
        sources, destination_root = settings_mod.validate_folders(settings)
        labels = scanner.source_labels(sources)
        suffix = settings.get("output_suffix", "")
        replace = bool(settings.get("replace_original"))
        rows = db.library_items_by_ids(ids)
        files, skipped = [], 0
        for row in rows:
            source = Path(row["source"]).resolve()
            try:
                stat = source.stat()
            except OSError:
                skipped += 1
                continue
            if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]:
                skipped += 1
                continue  # fichier modifié depuis l'analyse
            root = next((candidate for candidate in sources
                         if source == candidate or candidate in source.parents), None)
            if root is None:
                skipped += 1
                continue
            relative = Path(row["relative"])
            if (root / relative).resolve() != source:
                skipped += 1
                continue  # analyse faite avec une autre racine source
            ambiguous = bool(row["ambiguous"])
            if replace:
                output = source.parent / media.output_relative(Path(source.name), ambiguous, suffix).name
                if output != source and output.exists():
                    skipped += 1
                    continue
            else:
                base = destination_root / labels[root] if len(sources) > 1 else destination_root
                output = base / media.output_relative(relative, ambiguous, suffix)
                if output.exists():
                    skipped += 1
                    continue
            files.append({"source": str(source), "relative": relative.as_posix(),
                          "destination": str(output), "width": row["width"], "height": row["height"],
                          "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        result = scanner.enqueue(files, settings)
        result["skipped"] = skipped + max(0, len(ids) - len(rows))
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return error(str(exc))


@app.get("/api/previews")
def api_previews():
    with db.connect() as conn:
        rows = conn.execute("SELECT id,source,status,stage,progress,error,updated_at FROM jobs WHERE kind='preview' "
                            "ORDER BY created_at DESC LIMIT 20").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        manifest = config.preview_dir() / row["id"] / "manifest.json"
        try:
            item["manifest"] = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            item["manifest"] = None
        result.append(item)
    return jsonify({"previews": result})


@app.get("/api/previews/<job_id>/<name>")
def api_preview_file(job_id: str, name: str):
    if not valid_job_id(job_id) or not PREVIEW_FILE_RE.fullmatch(name):
        return error("Fichier invalide", 404)
    return send_from_directory(config.preview_dir() / job_id, name, conditional=True)


# ---------------------------------------------------------------- Ollama

@app.get("/api/models")
def api_models():
    return jsonify({"models": settings_mod.available_models(), "engine": config.NCNN_BIN,
                    "engine_present": Path(config.NCNN_BIN).exists(), "installs": converter.list_installs(),
                    "converter": converter.converter_status(), "suggestions": converter.SUGGESTIONS})


@app.post("/api/models")
def api_model_import():
    """Envoi depuis l'interface : paire ncnn (.param + .bin) ou fichier à convertir (.pth, .onnx, .zip...)."""
    param, binary, single = request.files.get("param"), request.files.get("bin"), request.files.get("file")
    form = request.form
    if single and single.filename:
        name = Path(single.filename).name
        if Path(name).suffix.lower() not in converter.MODEL_EXTENSIONS | {".zip"}:
            return error("Fichier attendu : .pth, .safetensors, .ckpt, .onnx ou .zip (ou la paire .param + .bin)")
        models.models_dir().mkdir(parents=True, exist_ok=True)
        folder = models.models_dir() / f".envoi-{uuid.uuid4().hex[:12]}"
        folder.mkdir()
        target = folder / (re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "modele.pth")
        single.save(target)
        try:
            task = converter.submit("file", name, label=form.get("label") or Path(name).stem,
                                    scale=form.get("scale") or None, tile=form.get("tile", 0), upload=target)
        except ValueError as exc:
            shutil.rmtree(folder, ignore_errors=True)
            return error(str(exc))
        return jsonify({"ok": True, "install": task}), 202
    if not param or not binary:
        return error("Envoie un fichier de modèle (.pth, .onnx...) ou les deux fichiers ncnn : .param et .bin")
    if not param.filename.lower().endswith(".param") or not binary.filename.lower().endswith(".bin"):
        return error("Fichiers attendus : un .param et un .bin (format ncnn)")
    try:
        meta = models.create(form.get("label") or Path(param.filename).stem, form.get("scale", 4),
                             form.get("tile", 0), param.stream, binary.stream)
        return jsonify({"ok": True, "model": meta}), 201
    except ValueError as exc:
        return error(str(exc))


@app.post("/api/models/install")
def api_model_install():
    """Installation depuis un lien : page OpenModelDB (ou son identifiant) ou lien direct vers le fichier."""
    try:
        payload = json_body()
        source = str(payload.get("source") or "").strip()
        if not source:
            return error("Colle le lien de la page OpenModelDB du modèle")
        if len(source) > 2000:
            return error("Lien trop long")
        kind = "openmodeldb"
        if source.startswith(("http://", "https://")) and "openmodeldb.info/models/" not in source:
            kind = "url"
        else:
            converter.openmodeldb_id(source)  # validation immédiate
        task = converter.submit(kind, source, label=str(payload.get("label") or ""), scale=payload.get("scale"),
                                tile=payload.get("tile", 0))
        return jsonify({"ok": True, "install": task}), 202
    except converter.InstallError as exc:
        return error(str(exc))
    except ValueError as exc:
        return error(str(exc))


@app.get("/api/models/installs/<install_id>")
def api_model_install_status(install_id: str):
    task = converter.get_install(install_id)
    return jsonify({"install": task}) if task else error("Installation inconnue", 404)


@app.post("/api/models/<model_id>/update")
def api_model_update(model_id: str):
    try:
        payload = json_body()
        return jsonify({"ok": True, "model": models.update(model_id, payload.get("label"), payload.get("scale"),
                                                           payload.get("tile", 0))})
    except LookupError as exc:
        return error(str(exc), 404)
    except ValueError as exc:
        return error(str(exc))


@app.post("/api/models/<model_id>/delete")
def api_model_delete(model_id: str):
    in_use = [k for k, v in (("réglages généraux", settings_mod.get_settings().get("model")),) if v == model_id]
    in_use += [p["path"] for p in settings_mod.get_settings().get("profiles", []) if p.get("model") == model_id]
    if in_use:
        return error("Modèle encore utilisé par : " + ", ".join(in_use) + ". Choisis un autre modèle d'abord.")
    try:
        models.delete(model_id)
        return jsonify({"ok": True})
    except LookupError as exc:
        return error(str(exc), 404)
    except ValueError as exc:
        return error(str(exc))


@app.get("/api/ollama/status")
def api_ollama_status():
    return jsonify(ollama.status(settings_mod.get_settings()))


@app.post("/api/ollama/test")
def api_ollama_test():
    try:
        payload = json_body()
    except ValueError as exc:
        return error(str(exc))
    base = str(payload.get("url", settings_mod.get_settings().get("ollama_url", ""))).strip().rstrip("/")
    if not base.startswith(("http://", "https://")) or len(base) > 500:
        return error("Adresse HTTP(S) invalide")
    try:
        data = ollama.get_json(base, "/api/tags", 5)
        version = ollama.get_json(base, "/api/version", 3).get("version", "inconnue")
        models = [item.get("name") for item in data.get("models", []) if item.get("name")]
        return jsonify({"ok": True, "url": base, "version": version, "models": models})
    except Exception:  # noqa: BLE001
        return error("Ollama ne répond pas à cette adresse (API /api/tags)", 502)


@app.post("/api/ollama/config")
def api_ollama_config():
    try:
        payload = {k: v for k, v in json_body().items() if k.startswith("ollama_")}
        saved = settings_mod.save_settings(payload)
        return jsonify({"ok": True, "enabled": saved["ollama_enabled"], "url": saved["ollama_url"],
                        "model": saved["ollama_model"]})
    except Exception as exc:  # noqa: BLE001
        return error(str(exc))


# ---------------------------------------------------------------- notifications et serveur multimédia

@app.post("/api/notify/test")
def api_notify_test():
    try:
        notify.send(settings_mod.get_settings(), "test", "Upscale : test", "Les notifications fonctionnent.")
        return jsonify({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return error(f"Échec de l'envoi : {str(exc)[:200]}", 502)


@app.post("/api/media/test")
def api_media_test():
    try:
        return jsonify({"ok": True, "message": notify.refresh_media_server(settings_mod.get_settings())})
    except Exception as exc:  # noqa: BLE001
        return error(f"Échec : {str(exc)[:200]}", 502)


# ---------------------------------------------------------------- santé

@app.get("/api/update")
def api_update():
    """Dernière version publiée sur GitHub (cache 30 min, `?refresh=1` pour forcer)."""
    return jsonify(updates.state(force=request.args.get("refresh") == "1"))


@app.get("/api/health")
def api_health():
    docker_needed = config.RUNNER != "local"
    docker = shutil.which("docker") is not None
    docker_ok = not docker_needed
    docker_error = None
    image_present = None
    if docker_needed and docker:
        try:
            info = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                                  capture_output=True, text=True, timeout=8)
            docker_ok = info.returncode == 0
            if not docker_ok:
                docker_error = info.stderr.strip()[-500:]
            else:
                inspect = subprocess.run(["docker", "image", "inspect", config.VIDEO2X_IMAGE],
                                         capture_output=True, timeout=8)
                image_present = inspect.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            docker_error = f"Docker ne répond pas : {exc}"
    elif not docker_needed:
        image_present = shutil.which(config.VIDEO2X_BIN) is not None
    return jsonify({
        "runner": config.RUNNER, "docker_cli": docker, "docker_ok": docker_ok, "error": docker_error,
        "image": config.VIDEO2X_IMAGE if docker_needed else config.VIDEO2X_BIN, "image_present": image_present,
        "gpu": gpu.get_state(), "version": config.VERSION,
    })


@app.post("/api/health/gpu")
def api_health_gpu():
    return jsonify({"ok": True, "started": gpu.start_check()}), 202


@app.get("/api/gpu/stats")
def api_gpu_stats():
    return jsonify({"cards": gpu.stats()})


def main() -> None:
    from waitress import serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if config.LEGACY_ENV:
        logging.warning("Variables à renommer en UPSCALE_* (les anciens noms seront retirés en 3.0) : %s",
                        ", ".join(sorted(set(config.LEGACY_ENV))))
    db.init_db()
    settings_mod.migrate()
    worker.recover_interrupted_jobs()
    converter.cleanup_staging()
    worker.start_background()
    serve(app, host="0.0.0.0", port=config.PORT, threads=8, ident="Upscale")
