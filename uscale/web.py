"""Interface web et API HTTP."""
from __future__ import annotations

import hmac
import json
import logging
import re
import shutil
import subprocess
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_from_directory

from . import config, db, gpu, media, notify, ollama, runner, scanner, worker
from . import settings as settings_mod

app = Flask(__name__, static_folder=str(config.STATIC_DIR), static_url_path="/static")
PREVIEW_FILE_RE = re.compile(r"[a-z0-9-]+\.(mp4|json)")


def error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


@app.before_request
def guard_request() -> Response | tuple[Response, int] | None:
    if request.path == "/api/ping":
        return None
    if config.AUTH_PASSWORD:
        auth = request.authorization
        valid = bool(auth and auth.type == "basic"
                     and hmac.compare_digest(str(auth.username or ""), config.AUTH_USER)
                     and hmac.compare_digest(str(auth.password or ""), config.AUTH_PASSWORD))
        if not valid:
            return Response("Authentification requise", 401, {"WWW-Authenticate": 'Basic realm="Uscale"'})
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        # Un JSON explicite force un preflight CORS : un site tiers ne peut pas piloter l'API.
        if not request.is_json:
            return error("Content-Type application/json requis", 415)
        origin = request.headers.get("Origin")
        if origin:
            allowed = {request.host, *config.TRUSTED_HOSTS}
            allowed.update(h.strip() for h in request.headers.get("X-Forwarded-Host", "").split(",") if h.strip())
            if urlsplit(origin).netloc not in allowed:
                return error("Origine refusée", 403)
    return None


def json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("Corps JSON invalide")
    return payload


def valid_job_id(job_id: str) -> bool:
    return bool(worker.JOB_ID_RE.fullmatch(job_id))


# ---------------------------------------------------------------- pages et réglages

@app.get("/")
def index():
    return send_from_directory(config.STATIC_DIR, "index.html")


@app.get("/api/ping")
def api_ping():
    return jsonify({"ok": True, "version": config.VERSION})


@app.get("/api/config")
def api_config():
    return jsonify({
        "settings": settings_mod.public_settings(settings_mod.get_settings()),
        "data_root": str(config.DATA_ROOT), "image": config.VIDEO2X_IMAGE, "runner": config.RUNNER,
        "version": config.VERSION, "segment_seconds": config.SEGMENT_SECONDS,
        "models": {k: {"label": settings_mod.MODEL_LABELS[k], "scales": list(v)} for k, v in settings_mod.MODEL_SCALES.items()},
    })


@app.post("/api/config")
def api_save_config():
    try:
        saved = settings_mod.save_settings(json_body())
        return jsonify({"ok": True, "settings": settings_mod.public_settings(saved)})
    except Exception as exc:  # noqa: BLE001
        return error(str(exc))


@app.get("/api/browse")
def api_browse():
    try:
        path = settings_mod.safe_path(request.args.get("path", str(config.DATA_ROOT)), must_exist=True)
        with_files = request.args.get("files") == "1"
        if path.is_file():
            path = path.parent
        if not path.is_dir():
            return jsonify({"error": "Ce chemin n'est pas un dossier"}), 400
        dirs, files = [], []
        for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            try:
                if child.is_symlink() or child.name.startswith(".uscale"):
                    continue
                if child.is_dir():
                    settings_mod.safe_path(str(child), must_exist=True)
                    dirs.append({"name": child.name, "path": str(child)})
                elif with_files and child.suffix.lower() in media.VIDEO_EXTENSIONS:
                    files.append({"name": child.name, "path": str(child), "size": child.stat().st_size})
            except (OSError, ValueError):
                continue
        return jsonify({"path": str(path), "parent": str(path.parent) if path != config.DATA_ROOT else None,
                        "directories": dirs, "files": files})
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
    where, params = ["kind=?"], [kind if kind in {"upscale", "preview"} else "upscale"]
    if status in STATUS_FILTERS:
        where.append("status=?")
        params.append(status)
    clause = " AND ".join(where)
    order = ("CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
             "CASE WHEN status='queued' THEN position END ASC, updated_at DESC")
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM jobs WHERE {clause}", params).fetchone()["n"]
        rows = conn.execute(f"SELECT * FROM jobs WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                            [*params, limit, (page - 1) * limit]).fetchall()
        counts = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status,COUNT(*) AS n FROM jobs WHERE kind='upscale' GROUP BY status").fetchall()}
    jobs = []
    for row in rows:
        item = dict(row)
        snapshot = json.loads(item.pop("settings_json") or "{}")
        item["model"] = item.get("resolved_model") or snapshot.get("model")
        item["output_height"] = snapshot.get("output_height")
        item["codec"] = snapshot.get("codec")
        item["profile"] = snapshot.get("profile")
        jobs.append(item)
    return jsonify({"jobs": jobs, "counts": counts, "total": total, "page": page, "limit": limit,
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
    if not valid_job_id(job_id) or not worker.cancel(job_id):
        return error("Tâche introuvable", 404)
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/retry")
def api_retry(job_id: str):
    try:
        worker.retry(job_id)
        worker.ensure_worker()
        return jsonify({"ok": True})
    except ValueError as exc:
        return error(str(exc))


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


@app.post("/api/jobs/<job_id>/move")
def api_move(job_id: str):
    try:
        worker.move(job_id, str(json_body().get("to", "")))
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
        models = [m for m in payload.get("models", []) if m in settings_mod.MODEL_SCALES]
        if not models:
            return error("Choisis au moins un modèle")
        job_id = worker.create_preview(source, start, duration, models)
        return jsonify({"ok": True, "id": job_id}), 202
    except (ValueError, OSError) as exc:
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
        notify.send(settings_mod.get_settings(), "test", "Uscale : test", "Les notifications fonctionnent.")
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
    db.init_db()
    worker.recover_interrupted_jobs()
    worker.start_background()
    serve(app, host="0.0.0.0", port=config.PORT, threads=8, ident="Uscale")
