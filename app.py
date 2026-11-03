from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

APP_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("USCALE_STATE_DIR", "/app/state"))
DB_PATH = STATE_DIR / "uscale.sqlite3"
LOG_DIR = STATE_DIR / "logs"
DATA_ROOT = Path(os.environ.get("USCALE_DATA_ROOT", "/data")).resolve()
VIDEO2X_IMAGE = os.environ.get("VIDEO2X_IMAGE", "ghcr.io/k4yt3x/video2x:6.4.0")
DEFAULT_SOURCE = os.environ.get("DEFAULT_SOURCE", "/data/videos")
DEFAULT_DESTINATION = os.environ.get("DEFAULT_DESTINATION", "/data/videos_upscaled")
VIDEO_GID = os.environ.get("VIDEO_GID", "44")
RENDER_GID = os.environ.get("RENDER_GID", "109")
PORT = int(os.environ.get("USCALE_PORT", "27000"))

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".vob"
}
app = Flask(__name__, static_folder="static")
worker_thread: threading.Thread | None = None
worker_wakeup = threading.Event()
active_process: subprocess.Popen | None = None
active_job_id: str | None = None
cancel_requested = False

DEFAULT_SETTINGS = {
    "source": DEFAULT_SOURCE,
    "destination": DEFAULT_DESTINATION,
    "max_input_height": 480,
    "output_height": 720,
    "model": "realesrgan-plus",
    "crf": 18,
    "preset": "medium",
    "include_subfolders": True,
    "ollama_enabled": False,
    "ollama_url": "http://host.docker.internal:11434",
    "ollama_model": "",
}

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def db() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
      CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        destination TEXT NOT NULL,
        source_size INTEGER NOT NULL,
        source_mtime_ns INTEGER NOT NULL,
        width INTEGER,
        height INTEGER,
        status TEXT NOT NULL,
        stage TEXT NOT NULL DEFAULT '',
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(source, source_size, source_mtime_ns)
      )
    """)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY CHECK(id=1),
        payload TEXT NOT NULL
      )
    """)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS app_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      )
    """)
    row = conn.execute("SELECT payload FROM settings WHERE id=1").fetchone()
    if not row:
        conn.execute("INSERT INTO settings(id,payload) VALUES(1,?)",
                     (json.dumps(DEFAULT_SETTINGS),))
    conn.commit()
    return conn

def get_settings(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    own = conn is None
    conn = conn or db()
    row = conn.execute("SELECT payload FROM settings WHERE id=1").fetchone()
    if own:
        conn.close()
    result = dict(DEFAULT_SETTINGS)
    if row:
        result.update(json.loads(row["payload"]))
    return result

def safe_path(raw: str, must_exist: bool = False) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Les chemins doivent être absolus")
    resolved = candidate.resolve(strict=must_exist)
    if resolved != DATA_ROOT and DATA_ROOT not in resolved.parents:
        raise ValueError(f"Chemin hors de la racine autorisée : {DATA_ROOT}")
    return resolved

def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    for key in ("source", "destination"):
        if key in payload:
            settings[key] = str(payload[key])
    for key, low, high in (
        ("max_input_height", 120, 2160),
        ("output_height", 240, 2160),
        ("crf", 12, 30),
    ):
        if key in payload:
            value = int(payload[key])
            if not low <= value <= high:
                raise ValueError(f"{key} doit être compris entre {low} et {high}")
            settings[key] = value
    if "model" in payload:
        if payload["model"] not in {"realesrgan-plus", "realesr-animevideov3"}:
            raise ValueError("Modèle non pris en charge")
        settings["model"] = payload["model"]
    if "preset" in payload:
        if payload["preset"] not in {"ultrafast", "fast", "medium", "slow", "slower"}:
            raise ValueError("Preset d'encodage invalide")
        settings["preset"] = payload["preset"]
    if "include_subfolders" in payload:
        settings["include_subfolders"] = bool(payload["include_subfolders"])
    if "ollama_enabled" in payload:
        settings["ollama_enabled"] = bool(payload["ollama_enabled"])
    if "ollama_url" in payload:
        url = str(payload["ollama_url"]).strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise ValueError("L'adresse Ollama doit commencer par http:// ou https://")
        if len(url) > 500:
            raise ValueError("Adresse Ollama trop longue")
        settings["ollama_url"] = url
    if "ollama_model" in payload:
        settings["ollama_model"] = str(payload["ollama_model"])[:200]

    source = safe_path(settings["source"], must_exist=True)
    dest = safe_path(settings["destination"], must_exist=False)
    if not source.is_dir():
        raise ValueError("Le dossier source doit exister et être un dossier")
    if dest == source or source in dest.parents:
        raise ValueError("La destination ne peut pas être dans le dossier source")
    if dest == DATA_ROOT or DATA_ROOT not in dest.parents:
        raise ValueError(f"La destination doit rester sous {DATA_ROOT}")
    conn = db()
    conn.execute("UPDATE settings SET payload=? WHERE id=1", (json.dumps(settings),))
    conn.commit()
    conn.close()
    return settings

def scan_files(settings: dict[str, Any]) -> list[dict[str, Any]]:
    source_root = safe_path(settings["source"], must_exist=True)
    destination_root = safe_path(settings["destination"], must_exist=False)
    iterator = source_root.rglob("*") if settings.get("include_subfolders", True) else source_root.iterdir()
    results = []
    for path in iterator:
        try:
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            resolved = path.resolve(strict=True)
            if source_root not in resolved.parents:
                continue
            stat = path.stat()
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "json", str(path)],
                capture_output=True, text=True, timeout=30
            )
            if probe.returncode:
                continue
            streams = json.loads(probe.stdout).get("streams", [])
            if not streams:
                continue
            width, height = int(streams[0]["width"]), int(streams[0]["height"])
            if height > int(settings["max_input_height"]) or height < 120:
                continue
            rel = path.relative_to(source_root)
            output = (destination_root / rel).with_suffix(".mkv")
            if output.exists():
                continue
            results.append({
                "source": str(path), "relative": rel.as_posix(),
                "destination": str(output), "width": width, "height": height,
                "size": stat.st_size, "mtime_ns": stat.st_mtime_ns
            })
        except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
            continue
    return results

def enqueue(files: list[dict[str, Any]]) -> int:
    conn = db()
    added = 0
    for item in files:
        signature = (item["source"], item["size"], item["mtime_ns"])
        existing = conn.execute(
            "SELECT id,status FROM jobs WHERE source=? AND source_size=? AND source_mtime_ns=?",
            signature
        ).fetchone()
        if existing:
            if existing["status"] in {"failed", "cancelled"}:
                conn.execute("""
                  UPDATE jobs SET status='queued',stage='En attente',error=NULL,
                    destination=?,width=?,height=?,created_at=?,updated_at=?
                  WHERE id=?
                """, (item["destination"], item["width"], item["height"],
                      now(), now(), existing["id"]))
                added += 1
            continue
        conn.execute("""
          INSERT INTO jobs
          (id,source,destination,source_size,source_mtime_ns,width,height,status,stage,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,'queued','En attente',?,?)
        """, (uuid.uuid4().hex[:12], item["source"], item["destination"],
              item["size"], item["mtime_ns"], item["width"], item["height"], now(), now()))
        added += 1
    conn.commit()
    conn.close()
    worker_wakeup.set()
    return added

def set_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    assignments = ",".join(f"{key}=?" for key in fields)
    conn = db()
    conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?",
                 (*fields.values(), job_id))
    conn.commit()
    conn.close()

def log_line(job_id: str, line: str) -> None:
    with (LOG_DIR / f"{job_id}.log").open("a", encoding="utf-8") as stream:
        stream.write(line.rstrip() + "\n")

def docker_command(job_id: str, source: Path, temp_output: Path,
                   settings: dict[str, Any]) -> list[str]:
    return [
        "docker", "run", "--rm", "--name", f"uscale-{job_id}",
        "--device", "/dev/dri:/dev/dri",
        "--group-add", VIDEO_GID, "--group-add", RENDER_GID,
        "-v", f"{DATA_ROOT}:{DATA_ROOT}",
        VIDEO2X_IMAGE,
        "-i", str(source), "-o", str(temp_output),
        "-p", "realesrgan", "-s", "4",
        "--realesrgan-model", settings["model"],
        "-e", f"crf={settings['crf']}",
        "-e", f"preset={settings['preset']}",
    ]

def run_job(job: sqlite3.Row) -> None:
    global active_process, active_job_id, cancel_requested
    job_id = job["id"]
    settings = get_settings()
    source = Path(job["source"])
    destination = Path(job["destination"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_ai = destination.with_name(destination.stem + f".{job_id}.ai-tmp.mkv")
    temp_final = destination.with_name(destination.stem + f".{job_id}.partial.mkv")
    temp_ai.unlink(missing_ok=True)
    temp_final.unlink(missing_ok=True)
    cancel_requested = False
    set_job(job_id, status="running", stage="Upscaling IA", error=None)
    log_line(job_id, f"[{now()}] Début : {source}")
    try:
        command = docker_command(job_id, source, temp_ai, settings)
        log_line(job_id, "$ " + " ".join(command))
        with (LOG_DIR / f"{job_id}.log").open("a", encoding="utf-8") as log:
            active_process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, text=True)
            while active_process.poll() is None:
                if cancel_requested:
                    subprocess.run(["docker", "stop", f"uscale-{job_id}"],
                                   capture_output=True, timeout=30)
                    active_process.terminate()
                    try:
                        active_process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        active_process.kill()
                    raise RuntimeError("Traitement annulé")
                time.sleep(1)
            code = active_process.returncode
        active_process = None
        if code != 0:
            raise RuntimeError(f"Video2X a échoué (code {code}); consulter le journal")
        if not temp_ai.exists() or temp_ai.stat().st_size == 0:
            raise RuntimeError("Video2X n'a pas créé de fichier de sortie")

        set_job(job_id, stage="Réencodage MKV / conservation audio et sous-titres")
        log_line(job_id, f"[{now()}] Réencodage final en hauteur {settings['output_height']}px")
        ffmpeg = [
            "ffmpeg", "-hide_banner", "-y", "-i", str(temp_ai), "-i", str(source),
            "-map", "0:v:0", "-map", "1:a?", "-map", "1:s?",
            "-map_metadata", "1", "-map_chapters", "1",
            "-vf", f"scale=-2:{settings['output_height']}:flags=lanczos",
            "-c:v", "libx264", "-preset", settings["preset"], "-crf", str(settings["crf"]),
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-c:s", "copy",
            "-max_muxing_queue_size", "2048", str(temp_final)
        ]
        with (LOG_DIR / f"{job_id}.log").open("a", encoding="utf-8") as log:
            result = subprocess.run(ffmpeg, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            raise RuntimeError("FFmpeg a échoué pendant le muxage final; consulter le journal")
        if not temp_final.exists() or temp_final.stat().st_size == 0:
            raise RuntimeError("Le fichier MKV final est vide")
        temp_final.replace(destination)
        temp_ai.unlink(missing_ok=True)
        set_job(job_id, status="completed", stage="Terminé")
        log_line(job_id, f"[{now()}] Terminé : {destination}")
    except Exception as exc:
        temp_ai.unlink(missing_ok=True)
        temp_final.unlink(missing_ok=True)
        status = "cancelled" if "annulé" in str(exc).lower() else "failed"
        set_job(job_id, status=status, stage="Arrêté" if status == "cancelled" else "Erreur",
                error=str(exc))
        log_line(job_id, f"[{now()}] ERREUR : {exc}")
    finally:
        active_process = None
        active_job_id = None
        cancel_requested = False

def worker_loop() -> None:
    global active_job_id
    while True:
        conn = db()
        paused = conn.execute("SELECT value FROM app_state WHERE key='paused'").fetchone()
        is_paused = bool(paused and paused["value"] == "1")
        job = None if is_paused else conn.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        conn.close()
        if job:
            active_job_id = job["id"]
            run_job(job)
            continue
        worker_wakeup.wait(timeout=5)
        worker_wakeup.clear()

def ensure_worker() -> None:
    global worker_thread
    if worker_thread is None or not worker_thread.is_alive():
        worker_thread = threading.Thread(target=worker_loop, daemon=True)
        worker_thread.start()

def recover_interrupted_jobs() -> None:
    conn = db()
    conn.execute("""
      UPDATE jobs SET status='queued',stage='Reprise après redémarrage',
        error='Le serveur a redémarré pendant ce traitement; tâche remise en file.',
        updated_at=? WHERE status='running'
    """, (now(),))
    conn.commit()
    conn.close()

@app.get("/")
def index():
    return send_from_directory(APP_DIR / "static", "index.html")

@app.get("/api/config")
def api_config():
    return jsonify({"settings": get_settings(), "data_root": str(DATA_ROOT)})

@app.post("/api/config")
def api_save_config():
    try:
        settings = save_settings(request.get_json(force=True) or {})
        return jsonify({"ok": True, "settings": settings})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

@app.get("/api/browse")
def api_browse():
    try:
        raw = request.args.get("path", str(DATA_ROOT))
        path = safe_path(raw, must_exist=True)
        if not path.is_dir():
            return jsonify({"error": "Ce chemin n'est pas un dossier"}), 400
        dirs = []
        for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            try:
                if child.is_dir() and not child.is_symlink():
                    safe_path(str(child), must_exist=True)
                    dirs.append({"name": child.name, "path": str(child)})
            except (OSError, ValueError):
                continue
        return jsonify({
            "path": str(path), "parent": str(path.parent) if path != DATA_ROOT else None,
            "directories": dirs
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

@app.post("/api/scan")
def api_scan():
    try:
        settings = save_settings(request.get_json(force=True) or {})
        files = scan_files(settings)
        return jsonify({"ok": True, "count": len(files), "files": files[:200]})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

@app.post("/api/queue")
def api_queue():
    try:
        settings = save_settings(request.get_json(force=True) or {})
        files = scan_files(settings)
        added = enqueue(files)
        ensure_worker()
        return jsonify({"ok": True, "found": len(files), "added": added})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

@app.get("/api/jobs")
def api_jobs():
    conn = db()
    rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 300").fetchall()
    counts = {row["status"]: row["n"] for row in conn.execute(
        "SELECT status,COUNT(*) AS n FROM jobs GROUP BY status"
    ).fetchall()}
    paused = conn.execute("SELECT value FROM app_state WHERE key='paused'").fetchone()
    conn.close()
    return jsonify({
        "jobs": [dict(row) for row in rows],
        "counts": counts, "paused": bool(paused and paused["value"] == "1"),
        "active_job_id": active_job_id
    })

@app.post("/api/pause")
def api_pause():
    conn = db()
    conn.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('paused','1')")
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.post("/api/resume")
def api_resume():
    conn = db()
    conn.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('paused','0')")
    conn.commit()
    conn.close()
    worker_wakeup.set()
    ensure_worker()
    return jsonify({"ok": True})

@app.post("/api/cancel/<job_id>")
def api_cancel(job_id: str):
    global cancel_requested
    conn = db()
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Job introuvable"}), 404
    if row["status"] == "queued":
        conn.execute("UPDATE jobs SET status='cancelled',stage='Annulé',updated_at=? WHERE id=?",
                     (now(), job_id))
    elif row["status"] == "running" and active_job_id == job_id:
        cancel_requested = True
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.get("/api/logs/<job_id>")
def api_logs(job_id: str):
    path = LOG_DIR / f"{job_id}.log"
    if not path.exists():
        return jsonify({"log": ""})
    try:
        return jsonify({"log": path.read_text(encoding="utf-8", errors="replace")[-30000:]})
    except OSError:
        return jsonify({"log": ""})

@app.get("/api/ollama/status")
def api_ollama_status():
    settings = get_settings()
    candidates = []
    configured = settings.get("ollama_url", "").strip().rstrip("/")
    if configured:
        candidates.append(configured)
    for candidate in ("http://host.docker.internal:11434", "http://172.17.0.1:11434"):
        if candidate not in candidates:
            candidates.append(candidate)
    errors = []
    for base in candidates:
        try:
            req = urllib.request.Request(base + "/api/tags", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            models = [item.get("name") for item in payload.get("models", []) if item.get("name")]
            version = "inconnue"
            try:
                with urllib.request.urlopen(base + "/api/version", timeout=2) as response:
                    version = json.loads(response.read().decode("utf-8")).get("version", "inconnue")
            except Exception:
                pass
            return jsonify({"enabled": True, "connected": True, "url": base,
                            "version": version, "models": models,
                            "selected_model": settings.get("ollama_model", "")})
        except Exception as exc:
            errors.append(f"{base}: {str(exc)[:160]}")
    return jsonify({"enabled": True, "connected": False,
                    "url": configured, "models": [],
                    "message": "Ollama non joignable depuis le conteneur Uscale",
                    "details": errors})

@app.post("/api/ollama/test")
def api_ollama_test():
    payload = request.get_json(force=True) or {}
    base = str(payload.get("url", get_settings().get("ollama_url", ""))).strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "Adresse HTTP(S) invalide"}), 400
    try:
        req = urllib.request.Request(base + "/api/tags", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        with urllib.request.urlopen(base + "/api/version", timeout=3) as response:
            version = json.loads(response.read().decode("utf-8")).get("version", "inconnue")
        models = [item.get("name") for item in data.get("models", []) if item.get("name")]
        return jsonify({"ok": True, "url": base, "version": version, "models": models})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:400]}), 502

@app.post("/api/ollama/config")
def api_ollama_config():
    try:
        settings = save_settings(request.get_json(force=True) or {})
        return jsonify({"ok": True, "enabled": settings["ollama_enabled"],
                        "url": settings["ollama_url"], "model": settings["ollama_model"]})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

@app.get("/api/health")
def api_health():
    docker = shutil.which("docker") is not None
    docker_ok = False
    docker_error = None
    if docker:
        check = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True, text=True, timeout=8)
        docker_ok = check.returncode == 0
        if not docker_ok:
            docker_error = check.stderr.strip()[-500:]
    gpu_names = []
    gpu_error = None
    if docker_ok:
        try:
            check = subprocess.run(
                ["docker", "run", "--rm", "--device", "/dev/dri",
                 VIDEO2X_IMAGE, "--list-gpus"],
                capture_output=True, text=True, timeout=35
            )
            if check.returncode == 0:
                gpu_names = [line.strip() for line in check.stdout.splitlines() if line.strip()]
            else:
                gpu_error = (check.stderr or check.stdout).strip()[-700:]
        except Exception as exc:
            gpu_error = str(exc)
    return jsonify({
        "docker_cli": docker, "docker_ok": docker_ok,
        "gpu_detected": bool(gpu_names), "gpu_output": gpu_names,
        "error": docker_error or gpu_error,
        "image": VIDEO2X_IMAGE
    })

if __name__ == "__main__":
    recover_interrupted_jobs()
    ensure_worker()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
