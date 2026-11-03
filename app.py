from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_from_directory

APP_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("USCALE_STATE_DIR", "/app/state"))
DB_PATH = STATE_DIR / "uscale.sqlite3"
LOG_DIR = STATE_DIR / "logs"
# Chemin de la bibliothèque vu depuis le conteneur Uscale.
DATA_ROOT = Path(os.environ.get("USCALE_DATA_ROOT", "/data")).resolve()
# Même dossier vu depuis l'hôte : c'est ce chemin que le daemon Docker doit monter
# dans le conteneur Video2X (le daemon ne voit que les chemins de l'hôte).
HOST_DATA = os.environ.get("USCALE_HOST_DATA", "").strip() or str(DATA_ROOT)
VIDEO2X_IMAGE = os.environ.get("VIDEO2X_IMAGE", "ghcr.io/k4yt3x/video2x:6.4.0")
VIDEO2X_GPU = os.environ.get("VIDEO2X_GPU", "").strip()
DEFAULT_SOURCE = os.environ.get("DEFAULT_SOURCE", "/data/videos")
DEFAULT_DESTINATION = os.environ.get("DEFAULT_DESTINATION", "/data/videos_upscaled")
VIDEO_GID = os.environ.get("VIDEO_GID", "44")
RENDER_GID = os.environ.get("RENDER_GID", "109")
PORT = int(os.environ.get("USCALE_PORT", "27000"))
AUTH_USER = os.environ.get("USCALE_USER", "admin")
AUTH_PASSWORD = os.environ.get("USCALE_PASSWORD", "")
# Hôtes supplémentaires autorisés comme origine des requêtes (ex. "uscale.maison.lan").
TRUSTED_ORIGINS = {h.strip() for h in os.environ.get("USCALE_TRUSTED_HOSTS", "").split(",") if h.strip()}
MIN_FREE_BYTES = int(float(os.environ.get("USCALE_MIN_FREE_GB", "5")) * 1024**3)
# Octets par pixel et par image estimés pour l'intermédiaire FFV1 (sans perte).
FFV1_BYTES_PER_PIXEL = float(os.environ.get("USCALE_FFV1_BYTES_PER_PIXEL", "0.75"))

CONTAINER_LABEL = "uscale.managed=1"

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".vob"
}
# Facteurs d'agrandissement disponibles pour chaque modèle Real-ESRGAN de Video2X.
MODEL_SCALES = {
    "realesrgan-plus": (4,),
    "realesrgan-plus-anime": (4,),
    "realesr-animevideov3": (2, 3, 4),
    "realesr-generalv3": (4,),
}
PRESETS = {"ultrafast", "fast", "medium", "slow", "slower"}
DEINTERLACE_MODES = {"auto", "on", "off"}
# Sous-titres que Matroska accepte tels quels.
MKV_SUBTITLE_COPY = {
    "subrip", "srt", "ass", "ssa", "webvtt", "text",
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle",
}
# Sous-titres texte à convertir en SRT (mov_text des MP4 notamment).
MKV_SUBTITLE_TO_SRT = {
    "mov_text", "microdvd", "subviewer", "subviewer1", "sami", "realtext",
    "jacosub", "mpl2", "pjs", "vplayer", "stl",
}
# Audio non accepté par Matroska : converti en FLAC (sans perte).
MKV_AUDIO_TO_FLAC = {"pcm_dvd", "pcm_bluray"}

app = Flask(__name__, static_folder="static")

state_lock = threading.Lock()
worker_thread: threading.Thread | None = None
worker_wakeup = threading.Event()
active_job_id: str | None = None
cancel_job_id: str | None = None

scan_state: dict[str, Any] = {"running": False}
gpu_state: dict[str, Any] = {"status": "never"}

DEFAULT_SETTINGS = {
    "source": DEFAULT_SOURCE,
    "destination": DEFAULT_DESTINATION,
    "max_input_height": 480,
    "output_height": 720,
    "model": "realesrgan-plus",
    "crf": 16,
    "preset": "slow",
    "include_subfolders": True,
    "deinterlace": "auto",
    "ollama_enabled": False,
    "ollama_url": "http://host.docker.internal:11434",
    "ollama_model": "",
}


class JobCancelled(Exception):
    pass


class JobError(Exception):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------- base de données

def init_db() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        for name, ddl in (("progress", "REAL"), ("eta_seconds", "INTEGER")):
            if name not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
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
        conn.execute("""
          CREATE TABLE IF NOT EXISTS probe_cache (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            width INTEGER,
            height INTEGER,
            updated_at TEXT NOT NULL
          )
        """)
        conn.execute("INSERT OR IGNORE INTO settings(id,payload) VALUES(1,?)",
                     (json.dumps(DEFAULT_SETTINGS),))
        conn.commit()
    finally:
        conn.close()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_settings() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT payload FROM settings WHERE id=1").fetchone()
    result = dict(DEFAULT_SETTINGS)
    if row:
        result.update(json.loads(row["payload"]))
    return result


def store_settings(settings: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(id,payload) VALUES(1,?)",
                     (json.dumps(settings),))


def is_paused() -> bool:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key='paused'").fetchone()
    return bool(row and row["value"] == "1")


def set_paused(value: bool) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('paused',?)",
                     ("1" if value else "0",))


# ---------------------------------------------------------------- paramètres

def safe_path(raw: str, must_exist: bool = False) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Les chemins doivent être absolus")
    resolved = candidate.resolve(strict=must_exist)
    if resolved != DATA_ROOT and DATA_ROOT not in resolved.parents:
        raise ValueError(f"Chemin hors de la racine autorisée : {DATA_ROOT}")
    return resolved


def apply_ollama_settings(settings: dict[str, Any], payload: dict[str, Any]) -> None:
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


def validate_folders(settings: dict[str, Any]) -> tuple[Path, Path]:
    source = safe_path(settings["source"], must_exist=True)
    dest = safe_path(settings["destination"], must_exist=False)
    if not source.is_dir():
        raise ValueError("Le dossier source doit exister et être un dossier")
    if dest == source or source in dest.parents:
        raise ValueError("La destination ne peut pas être dans le dossier source")
    if dest in source.parents:
        raise ValueError("Le dossier source ne peut pas être dans la destination")
    if dest == DATA_ROOT or DATA_ROOT not in dest.parents:
        raise ValueError(f"La destination doit rester sous {DATA_ROOT}")
    return source, dest


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
        if payload["model"] not in MODEL_SCALES:
            raise ValueError("Modèle non pris en charge")
        settings["model"] = payload["model"]
    if "preset" in payload:
        if payload["preset"] not in PRESETS:
            raise ValueError("Preset d'encodage invalide")
        settings["preset"] = payload["preset"]
    if "include_subfolders" in payload:
        settings["include_subfolders"] = bool(payload["include_subfolders"])
    if "deinterlace" in payload:
        if payload["deinterlace"] not in DEINTERLACE_MODES:
            raise ValueError("Mode de désentrelacement invalide")
        settings["deinterlace"] = payload["deinterlace"]
    apply_ollama_settings(settings, payload)
    validate_folders(settings)
    store_settings(settings)
    return settings


def save_ollama_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Enregistre uniquement les réglages Ollama, sans dépendre des dossiers vidéo."""
    settings = get_settings()
    apply_ollama_settings(settings, payload)
    store_settings(settings)
    return settings


# ---------------------------------------------------------------- analyse des fichiers

def ffprobe_json(path: Path, *args: str, timeout: int = 30) -> dict[str, Any]:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", *args, "-of", "json", str(path)],
        capture_output=True, text=True, timeout=timeout,
    )
    if probe.returncode:
        raise JobError(f"ffprobe a échoué sur {path.name} : {probe.stderr.strip()[-300:]}")
    return json.loads(probe.stdout or "{}")


def probe_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        data = ffprobe_json(path, "-select_streams", "v:0", "-show_entries", "stream=width,height")
        streams = data.get("streams", [])
        if not streams:
            return None
        return int(streams[0]["width"]), int(streams[0]["height"])
    except (JobError, OSError, ValueError, KeyError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def parse_ratio(raw: Any, default: float = 0.0) -> float:
    try:
        text = str(raw)
        if ":" in text or "/" in text:
            num, den = re.split(r"[:/]", text, maxsplit=1)
            return float(num) / float(den) if float(den) else default
        return float(text)
    except (TypeError, ValueError):
        return default


def probe_source(path: Path) -> dict[str, Any]:
    """Informations nécessaires au traitement d'une source."""
    data = ffprobe_json(path, "-show_entries",
                        "stream=index,codec_type,codec_name,width,height,sample_aspect_ratio,"
                        "field_order,avg_frame_rate,r_frame_rate,nb_frames:format=duration")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise JobError("Aucune piste vidéo dans la source")
    width, height = int(video["width"]), int(video["height"])
    sar = parse_ratio(video.get("sample_aspect_ratio"), 1.0) or 1.0
    fps = parse_ratio(video.get("avg_frame_rate")) or parse_ratio(video.get("r_frame_rate")) or 25.0
    duration = parse_ratio(data.get("format", {}).get("duration"))
    frames = int(parse_ratio(video.get("nb_frames"))) or int(duration * fps)
    return {
        "width": width, "height": height, "sar": sar, "fps": fps,
        "duration": duration, "frames": frames,
        "field_order": str(video.get("field_order", "unknown")),
        "audio": [s.get("codec_name", "") for s in streams if s.get("codec_type") == "audio"],
        "subtitles": [s.get("codec_name", "") for s in streams if s.get("codec_type") == "subtitle"],
    }


def output_relative(rel: Path, ambiguous: bool) -> Path:
    """film.avi -> film.mkv, ou film.avi.mkv si un autre film.* existe à côté."""
    if ambiguous:
        return rel.with_name(rel.name + ".mkv")
    return rel.with_suffix(".mkv")


def list_candidates(source_root: Path, recursive: bool) -> list[Path]:
    iterator = source_root.rglob("*") if recursive else source_root.iterdir()
    found = []
    for path in iterator:
        try:
            if path.is_symlink() or path.suffix.lower() not in VIDEO_EXTENSIONS or not path.is_file():
                continue
            if source_root not in path.resolve(strict=True).parents:
                continue
            found.append(path)
        except OSError:
            continue
    found.sort()
    return found


def cached_dimensions(conn: sqlite3.Connection, path: Path, stat: os.stat_result) -> tuple[int, int] | None:
    row = conn.execute("SELECT size,mtime_ns,width,height FROM probe_cache WHERE path=?",
                       (str(path),)).fetchone()
    if row and row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
        return (row["width"], row["height"]) if row["width"] else None
    dims = probe_dimensions(path)
    conn.execute("""INSERT OR REPLACE INTO probe_cache(path,size,mtime_ns,width,height,updated_at)
                    VALUES(?,?,?,?,?,?)""",
                 (str(path), stat.st_size, stat.st_mtime_ns,
                  dims[0] if dims else None, dims[1] if dims else None, now()))
    return dims


def scan_files(settings: dict[str, Any], progress: Callable[[int, int], None] | None = None
               ) -> dict[str, Any]:
    source_root, destination_root = validate_folders(settings)
    candidates = list_candidates(source_root, settings.get("include_subfolders", True))
    stems = Counter((p.parent, p.stem.lower()) for p in candidates)
    files: list[dict[str, Any]] = []
    already_known = 0
    with connect() as conn:
        known = {
            (row["source"], row["source_size"], row["source_mtime_ns"]): row["status"]
            for row in conn.execute("SELECT source,source_size,source_mtime_ns,status FROM jobs")
        }
        for index, path in enumerate(candidates, 1):
            if progress and (index % 10 == 0 or index == len(candidates)):
                progress(index, len(candidates))
            try:
                stat = path.stat()
                dims = cached_dimensions(conn, path, stat)
                if not dims:
                    continue
                width, height = dims
                if height > int(settings["max_input_height"]) or height < 120:
                    continue
                rel = path.relative_to(source_root)
                output = destination_root / output_relative(rel, stems[(path.parent, path.stem.lower())] > 1)
                if output.exists():
                    continue
                if known.get((str(path), stat.st_size, stat.st_mtime_ns)) in {"queued", "running", "completed"}:
                    already_known += 1
                    continue
                files.append({
                    "source": str(path), "relative": rel.as_posix(),
                    "destination": str(output), "width": width, "height": height,
                    "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                })
            except (OSError, ValueError):
                continue
            if index % 50 == 0:
                conn.commit()
    return {"files": files, "already_known": already_known, "checked": len(candidates)}


def enqueue(files: list[dict[str, Any]]) -> dict[str, int]:
    added = conflicts = 0
    with connect() as conn:
        active_destinations = {
            row["destination"]: row["source"]
            for row in conn.execute("SELECT source,destination FROM jobs WHERE status IN ('queued','running')")
        }
        for item in files:
            owner = active_destinations.get(item["destination"])
            if owner and owner != item["source"]:
                conflicts += 1
                continue
            signature = (item["source"], item["size"], item["mtime_ns"])
            existing = conn.execute(
                "SELECT id,status FROM jobs WHERE source=? AND source_size=? AND source_mtime_ns=?",
                signature,
            ).fetchone()
            if existing:
                if existing["status"] in {"failed", "cancelled"}:
                    conn.execute("""
                      UPDATE jobs SET status='queued',stage='En attente',error=NULL,progress=NULL,
                        eta_seconds=NULL,destination=?,width=?,height=?,created_at=?,updated_at=?
                      WHERE id=?
                    """, (item["destination"], item["width"], item["height"], now(), now(), existing["id"]))
                    added += 1
                    active_destinations[item["destination"]] = item["source"]
                continue
            conn.execute("""
              INSERT INTO jobs
              (id,source,destination,source_size,source_mtime_ns,width,height,status,stage,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,'queued','En attente',?,?)
            """, (uuid.uuid4().hex[:12], item["source"], item["destination"],
                  item["size"], item["mtime_ns"], item["width"], item["height"], now(), now()))
            active_destinations[item["destination"]] = item["source"]
            added += 1
    worker_wakeup.set()
    return {"added": added, "conflicts": conflicts}


def run_scan(settings: dict[str, Any], enqueue_files: bool) -> None:
    def progress(done: int, total: int) -> None:
        with state_lock:
            scan_state.update(checked=done, total=total)

    try:
        result = scan_files(settings, progress)
        update: dict[str, Any] = {
            "found": len(result["files"]), "already_known": result["already_known"],
            "checked": result["checked"], "total": result["checked"],
            "files": [f["relative"] for f in result["files"][:200]],
        }
        if enqueue_files:
            update.update(enqueue(result["files"]))
            ensure_worker()
        with state_lock:
            scan_state.update(update, running=False, finished_at=now())
    except Exception as exc:  # noqa: BLE001 - l'erreur est remontée à l'interface
        with state_lock:
            scan_state.update(running=False, error=str(exc), finished_at=now())


def start_scan(settings: dict[str, Any], enqueue_files: bool) -> bool:
    with state_lock:
        if scan_state.get("running"):
            return False
        scan_state.clear()
        scan_state.update(running=True, enqueue=enqueue_files, checked=0, total=0,
                          started_at=now(), error=None)
    threading.Thread(target=run_scan, args=(settings, enqueue_files), daemon=True).start()
    return True


# ---------------------------------------------------------------- jobs

def set_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    assignments = ",".join(f"{key}=?" for key in fields)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*fields.values(), job_id))


def log_line(job_id: str, line: str) -> None:
    with (LOG_DIR / f"{job_id}.log").open("a", encoding="utf-8") as stream:
        stream.write(line.rstrip() + "\n")


def choose_scale(model: str, source_height: int, output_height: int) -> int:
    """Plus petit facteur du modèle qui atteint la hauteur demandée (inutile de calculer ×4 pour 480→720)."""
    scales = MODEL_SCALES.get(model, (4,))
    needed = output_height / max(source_height, 1)
    for scale in scales:
        if scale >= needed:
            return scale
    return scales[-1]


def output_size(info: dict[str, Any], output_height: int) -> tuple[int, int]:
    """Taille finale en pixels carrés qui respecte le format d'affichage (DVD anamorphiques)."""
    display_aspect = info["width"] * info["sar"] / info["height"]
    width = max(2, int(round(output_height * display_aspect / 2)) * 2)
    return width, output_height


def needs_deinterlace(mode: str, info: dict[str, Any]) -> bool:
    if mode == "on":
        return True
    if mode == "off":
        return False
    return info.get("field_order") in {"tt", "bb", "tb", "bt"}


def estimate_intermediate_bytes(info: dict[str, Any], scale: int) -> int:
    pixels = info["width"] * scale * info["height"] * scale
    return int(pixels * FFV1_BYTES_PER_PIXEL * max(info["frames"], 1))


def human_size(value: float) -> str:
    return f"{value / 1024**3:.1f} Go"


def container_name(job_id: str) -> str:
    return f"uscale-{job_id}"


def docker_command(job_id: str, source: Path, temp_output: Path,
                   settings: dict[str, Any], scale: int = 4) -> list[str]:
    command = [
        "docker", "run", "--rm", "--name", container_name(job_id),
        "--label", CONTAINER_LABEL, "--label", f"uscale.job={job_id}",
        "--device", "/dev/dri:/dev/dri",
        "--group-add", VIDEO_GID, "--group-add", RENDER_GID,
        # Le daemon Docker interprète la source du montage comme un chemin de l'hôte.
        "-v", f"{HOST_DATA}:{DATA_ROOT}",
        VIDEO2X_IMAGE,
        "-i", str(source), "-o", str(temp_output),
        "-p", "realesrgan", "-s", str(scale),
        "--realesrgan-model", settings["model"],
        # Intermédiaire sans perte : FFmpeg réalise l'unique encodage avec perte.
        "-c", "ffv1",
    ]
    if VIDEO2X_GPU:
        command += ["-g", VIDEO2X_GPU]
    return command


def subtitle_args(codecs: list[str]) -> tuple[list[str], list[str]]:
    """Arguments FFmpeg pour les sous-titres + liste des pistes ignorées."""
    args: list[str] = []
    skipped: list[str] = []
    out = 0
    for index, codec in enumerate(codecs):
        if codec in MKV_SUBTITLE_COPY:
            args += ["-map", f"1:s:{index}", f"-c:s:{out}", "copy"]
        elif codec in MKV_SUBTITLE_TO_SRT:
            args += ["-map", f"1:s:{index}", f"-c:s:{out}", "srt"]
        else:
            skipped.append(f"#{index} ({codec or 'inconnu'})")
            continue
        out += 1
    return args, skipped


def audio_args(codecs: list[str]) -> list[str]:
    args = ["-map", "1:a?", "-c:a", "copy"]
    for index, codec in enumerate(codecs):
        if codec in MKV_AUDIO_TO_FLAC:
            args += [f"-c:a:{index}", "flac"]
    return args


def ffmpeg_final_command(temp_ai: Path, source: Path, temp_final: Path,
                         info: dict[str, Any], settings: dict[str, Any]) -> tuple[list[str], list[str]]:
    width, height = output_size(info, int(settings["output_height"]))
    subs, skipped = subtitle_args(info.get("subtitles", []))
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-nostats", "-progress", "pipe:1",
        "-i", str(temp_ai), "-i", str(source),
        "-map", "0:v:0", *audio_args(info.get("audio", [])), *subs,
        "-map", "1:t?", "-c:t", "copy",
        "-map_metadata", "1", "-map_chapters", "1",
        "-vf", f"scale={width}:{height}:flags=lanczos+accurate_rnd+full_chroma_int,setsar=1",
        "-c:v", "libx264", "-preset", settings["preset"], "-crf", str(settings["crf"]),
        "-pix_fmt", "yuv420p",
        "-max_muxing_queue_size", "2048", str(temp_final),
    ]
    return command, skipped


def ffmpeg_deinterlace_command(source: Path, temp_deint: Path) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-nostats", "-progress", "pipe:1",
        "-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", "bwdif=mode=send_frame", "-c:v", "ffv1", str(temp_deint),
    ]


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FRAME_RE = re.compile(r"frames?\D{0,20}?(\d+)\s*/\s*(\d+)", re.IGNORECASE)
PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
OUT_TIME_RE = re.compile(r"^out_time_(?:us|ms)=(\d+)$")


def parse_video2x_progress(line: str) -> float | None:
    clean = ANSI_RE.sub("", line)
    match = FRAME_RE.search(clean)
    if match and int(match.group(2)) > 0:
        return min(int(match.group(1)) / int(match.group(2)), 1.0)
    match = PERCENT_RE.search(clean)
    if match and "frame" in clean.lower():
        return min(float(match.group(1)) / 100, 1.0)
    return None


def ffmpeg_progress_parser(duration: float) -> Callable[[str], float | None]:
    def parse(line: str) -> float | None:
        match = OUT_TIME_RE.match(line.strip())
        if match and duration > 0:
            return min(int(match.group(1)) / 1_000_000 / duration, 1.0)
        return None
    return parse


def is_cancel_requested(job_id: str) -> bool:
    with state_lock:
        return cancel_job_id == job_id


def docker_remove(name: str) -> None:
    try:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass


def stop_process(proc: subprocess.Popen, container: str | None) -> None:
    if container:
        try:
            subprocess.run(["docker", "stop", "-t", "10", container], capture_output=True, timeout=40)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    if container:
        docker_remove(container)


def run_process(job_id: str, command: list[str], *, container: str | None = None,
                parse_progress: Callable[[str], float | None] | None = None,
                disk_dir: Path | None = None) -> int:
    """Lance une commande, journalise sa sortie, suit la progression et l'annulation."""
    log_line(job_id, "$ " + " ".join(command))
    stage_started = time.monotonic()
    last = {"db": 0.0, "log": 0.0, "progress": -1.0}

    def handle(line: str) -> None:
        if not line.strip():
            return
        value = parse_progress(line) if parse_progress else None
        current = time.monotonic()
        if value is None:
            if not line.startswith(("frame=", "fps=", "stream_", "bitrate=", "total_size=", "out_time",
                                    "dup_frames=", "drop_frames=", "speed=", "progress=")):
                log_line(job_id, ANSI_RE.sub("", line))
            return
        if current - last["log"] >= 60:
            log_line(job_id, f"[{now()}] progression {value * 100:.1f} %")
            last["log"] = current
        if current - last["db"] >= 3 and abs(value - last["progress"]) >= 0.001:
            elapsed = current - stage_started
            eta = int(elapsed * (1 - value) / value) if value > 0.005 else None
            set_job(job_id, progress=round(value, 4), eta_seconds=eta)
            last.update(db=current, progress=value)

    def reader(stream: Any) -> None:
        buffer = b""
        for chunk in iter(lambda: stream.read1(65536), b""):
            buffer += chunk
            parts = re.split(rb"[\r\n]", buffer)
            buffer = parts.pop()
            for part in parts:
                handle(part.decode("utf-8", errors="replace"))
        if buffer:
            handle(buffer.decode("utf-8", errors="replace"))

    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    thread = threading.Thread(target=reader, args=(proc.stdout,), daemon=True)
    thread.start()
    last_disk_check = 0.0
    try:
        while proc.poll() is None:
            if is_cancel_requested(job_id):
                raise JobCancelled("Traitement annulé")
            if disk_dir and time.monotonic() - last_disk_check >= 15:
                last_disk_check = time.monotonic()
                free = shutil.disk_usage(disk_dir).free
                if free < MIN_FREE_BYTES:
                    raise JobError(f"Espace disque presque plein ({human_size(free)} libres) : traitement arrêté")
            time.sleep(0.5)
    except BaseException:
        stop_process(proc, container)
        raise
    finally:
        thread.join(timeout=10)
    return proc.returncode


def ensure_image(job_id: str) -> None:
    inspect = subprocess.run(["docker", "image", "inspect", VIDEO2X_IMAGE], capture_output=True, timeout=30)
    if inspect.returncode == 0:
        return
    set_job(job_id, stage="Téléchargement de l'image Video2X", progress=None, eta_seconds=None)
    code = run_process(job_id, ["docker", "pull", VIDEO2X_IMAGE])
    if code != 0:
        raise JobError(f"Impossible de télécharger {VIDEO2X_IMAGE}; consulter le journal")


def check_disk_space(destination: Path, info: dict[str, Any], scale: int, deinterlace: bool) -> int:
    needed = estimate_intermediate_bytes(info, scale)
    if deinterlace:
        needed += estimate_intermediate_bytes(info, 1)
    free = shutil.disk_usage(destination.parent).free
    if free - needed < MIN_FREE_BYTES:
        raise JobError(
            f"Espace disque insuffisant : l'intermédiaire sans perte est estimé à {human_size(needed)}, "
            f"{human_size(free)} libres (marge de {human_size(MIN_FREE_BYTES)} conservée)"
        )
    return needed


def run_job(job: sqlite3.Row) -> None:
    global cancel_job_id
    job_id = job["id"]
    settings = get_settings()
    source = Path(job["source"])
    destination = Path(job["destination"])
    temp_ai = destination.with_name(destination.stem + f".{job_id}.ai-tmp.mkv")
    temp_deint = destination.with_name(destination.stem + f".{job_id}.deint-tmp.mkv")
    temp_final = destination.with_name(destination.stem + f".{job_id}.partial.mkv")
    temps = (temp_ai, temp_deint, temp_final)
    log_line(job_id, f"[{now()}] Début : {source}")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        for temp in temps:
            temp.unlink(missing_ok=True)
        if destination.exists():
            raise JobError(f"La destination existe déjà, elle n'est pas écrasée : {destination}")
        info = probe_source(source)
        scale = choose_scale(settings["model"], info["height"], int(settings["output_height"]))
        deinterlace = needs_deinterlace(settings.get("deinterlace", "auto"), info)
        needed = check_disk_space(destination, info, scale, deinterlace)
        log_line(job_id, f"[{now()}] Source {info['width']}×{info['height']} (SAR {info['sar']:.3f}, "
                         f"{info['field_order']}), facteur IA ×{scale}, intermédiaire estimé {human_size(needed)}")

        ensure_image(job_id)
        ai_input = source
        if deinterlace:
            set_job(job_id, stage="Désentrelacement", progress=None, eta_seconds=None)
            code = run_process(job_id, ffmpeg_deinterlace_command(source, temp_deint),
                               parse_progress=ffmpeg_progress_parser(info["duration"]), disk_dir=destination.parent)
            if code != 0:
                raise JobError("Le désentrelacement a échoué; consulter le journal")
            ai_input = temp_deint

        set_job(job_id, stage=f"Upscaling IA ×{scale}", progress=None, eta_seconds=None)
        docker_remove(container_name(job_id))
        code = run_process(job_id, docker_command(job_id, ai_input, temp_ai, settings, scale),
                           container=container_name(job_id), parse_progress=parse_video2x_progress,
                           disk_dir=destination.parent)
        if code != 0:
            raise JobError(f"Video2X a échoué (code {code}); consulter le journal")
        if not temp_ai.exists() or temp_ai.stat().st_size == 0:
            raise JobError("Video2X n'a pas créé de fichier de sortie")
        temp_deint.unlink(missing_ok=True)

        set_job(job_id, stage="Réencodage MKV / conservation audio et sous-titres", progress=None, eta_seconds=None)
        command, skipped = ffmpeg_final_command(temp_ai, source, temp_final, info, settings)
        if skipped:
            log_line(job_id, f"[{now()}] Sous-titres non compatibles MKV ignorés : {', '.join(skipped)}")
        final_width, final_height = output_size(info, int(settings["output_height"]))
        log_line(job_id, f"[{now()}] Réencodage final en {final_width} × {final_height}")
        code = run_process(job_id, command, parse_progress=ffmpeg_progress_parser(info["duration"]),
                           disk_dir=destination.parent)
        if code != 0:
            raise JobError("FFmpeg a échoué pendant le muxage final; consulter le journal")
        if not temp_final.exists() or temp_final.stat().st_size == 0:
            raise JobError("Le fichier MKV final est vide")
        if destination.exists():
            raise JobError(f"La destination a été créée pendant le traitement, elle n'est pas écrasée : {destination}")
        temp_final.replace(destination)
        temp_ai.unlink(missing_ok=True)
        set_job(job_id, status="completed", stage="Terminé", progress=1.0, eta_seconds=None)
        log_line(job_id, f"[{now()}] Terminé : {destination}")
    except JobCancelled as exc:
        set_job(job_id, status="cancelled", stage="Arrêté", error=str(exc), eta_seconds=None)
        log_line(job_id, f"[{now()}] {exc}")
    except Exception as exc:  # noqa: BLE001 - toute erreur doit marquer le job en échec
        set_job(job_id, status="failed", stage="Erreur", error=str(exc), eta_seconds=None)
        log_line(job_id, f"[{now()}] ERREUR : {exc}")
    finally:
        for temp in temps:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        with state_lock:
            if cancel_job_id == job_id:
                cancel_job_id = None


def claim_next_job() -> sqlite3.Row | None:
    """Réserve atomiquement le prochain job : une annulation concurrente ne peut pas être écrasée."""
    global active_job_id
    with connect() as conn:
        candidate = conn.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
    if not candidate:
        return None
    with state_lock:
        active_job_id = candidate["id"]
    with connect() as conn:
        claimed = conn.execute(
            "UPDATE jobs SET status='running',stage='Préparation',error=NULL,progress=NULL,eta_seconds=NULL,"
            "updated_at=? WHERE id=? AND status='queued'",
            (now(), candidate["id"]),
        ).rowcount
    if not claimed:
        with state_lock:
            active_job_id = None
        return None
    return candidate


def worker_loop() -> None:
    global active_job_id
    while True:
        try:
            job = None if is_paused() else claim_next_job()
            if job:
                try:
                    run_job(job)
                finally:
                    with state_lock:
                        active_job_id = None
                continue
        except Exception as exc:  # noqa: BLE001 - le worker ne doit jamais mourir
            app.logger.exception("Erreur du worker : %s", exc)
            time.sleep(5)
        worker_wakeup.wait(timeout=5)
        worker_wakeup.clear()


def ensure_worker() -> None:
    global worker_thread
    with state_lock:
        if worker_thread is None or not worker_thread.is_alive():
            worker_thread = threading.Thread(target=worker_loop, daemon=True)
            worker_thread.start()


def cleanup_orphan_containers() -> None:
    """Un redémarrage d'Uscale n'arrête pas les conteneurs Video2X frères : on les supprime."""
    if not shutil.which("docker"):
        return
    try:
        listing = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={CONTAINER_LABEL}"],
                                 capture_output=True, text=True, timeout=30)
        for container_id in listing.stdout.split():
            docker_remove(container_id)
    except (OSError, subprocess.TimeoutExpired):
        pass


def recover_interrupted_jobs() -> None:
    cleanup_orphan_containers()
    with connect() as conn:
        interrupted = [row["id"] for row in conn.execute("SELECT id FROM jobs WHERE status='running'")]
        conn.execute("""
          UPDATE jobs SET status='queued',stage='Reprise après redémarrage',progress=NULL,eta_seconds=NULL,
            error='Le serveur a redémarré pendant ce traitement; tâche remise en file.',
            updated_at=? WHERE status='running'
        """, (now(),))
    if shutil.which("docker"):
        for job_id in interrupted:
            docker_remove(container_name(job_id))


# ---------------------------------------------------------------- santé / GPU

def gpu_check_command() -> list[str]:
    command = [
        "docker", "run", "--rm", "--label", CONTAINER_LABEL,
        "--device", "/dev/dri:/dev/dri",
        "--group-add", VIDEO_GID, "--group-add", RENDER_GID,
        VIDEO2X_IMAGE, "--list-gpus",
    ]
    return command


def run_gpu_check() -> None:
    try:
        check = subprocess.run(gpu_check_command(), capture_output=True, text=True, timeout=1800)
        lines = [line.strip() for line in check.stdout.splitlines() if line.strip()]
        result = {"status": "ok" if check.returncode == 0 and lines else "error", "devices": lines,
                  "error": None if check.returncode == 0 else (check.stderr or check.stdout).strip()[-700:]}
    except Exception as exc:  # noqa: BLE001
        result = {"status": "error", "devices": [], "error": str(exc)}
    with state_lock:
        gpu_state.clear()
        gpu_state.update(result, checked_at=now())


def start_gpu_check() -> bool:
    with state_lock:
        if gpu_state.get("status") == "running":
            return False
        gpu_state.clear()
        gpu_state.update(status="running", started_at=now())
    threading.Thread(target=run_gpu_check, daemon=True).start()
    return True


# ---------------------------------------------------------------- sécurité HTTP

@app.before_request
def guard_request() -> Response | tuple[Response, int] | None:
    if request.path == "/api/ping":
        return None
    if AUTH_PASSWORD:
        auth = request.authorization
        valid = bool(auth and auth.type == "basic"
                     and hmac.compare_digest(str(auth.username or ""), AUTH_USER)
                     and hmac.compare_digest(str(auth.password or ""), AUTH_PASSWORD))
        if not valid:
            return Response("Authentification requise", 401, {"WWW-Authenticate": 'Basic realm="Uscale"'})
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        # Un JSON explicite force un preflight CORS : un site tiers ne peut pas piloter l'API.
        if not request.is_json:
            return jsonify({"ok": False, "error": "Content-Type application/json requis"}), 415
        origin = request.headers.get("Origin")
        if origin:
            # Derrière un reverse proxy, l'hôte public arrive dans X-Forwarded-Host.
            allowed = {request.host, *TRUSTED_ORIGINS}
            allowed.update(h.strip() for h in request.headers.get("X-Forwarded-Host", "").split(",") if h.strip())
            if urlsplit(origin).netloc not in allowed:
                return jsonify({"ok": False, "error": "Origine refusée"}), 403
    return None


def json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("Corps JSON invalide")
    return payload


# ---------------------------------------------------------------- routes

@app.get("/")
def index():
    return send_from_directory(APP_DIR / "static", "index.html")


@app.get("/api/ping")
def api_ping():
    return jsonify({"ok": True})


@app.get("/api/config")
def api_config():
    return jsonify({"settings": get_settings(), "data_root": str(DATA_ROOT),
                    "image": VIDEO2X_IMAGE, "models": {k: list(v) for k, v in MODEL_SCALES.items()}})


@app.post("/api/config")
def api_save_config():
    try:
        return jsonify({"ok": True, "settings": save_settings(json_body())})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/browse")
def api_browse():
    try:
        raw = request.args.get("path", str(DATA_ROOT))
        path = safe_path(raw, must_exist=True)
        if not path.is_dir():
            return jsonify({"error": "Ce chemin n'est pas un dossier"}), 400
        dirs = []
        for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            try:
                if child.is_dir() and not child.is_symlink():
                    safe_path(str(child), must_exist=True)
                    dirs.append({"name": child.name, "path": str(child)})
            except (OSError, ValueError):
                continue
        return jsonify({
            "path": str(path), "parent": str(path.parent) if path != DATA_ROOT else None,
            "directories": dirs,
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


def start_scan_from_request(enqueue_files: bool):
    try:
        settings = save_settings(json_body())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not start_scan(settings, enqueue_files):
        return jsonify({"ok": False, "error": "Une analyse est déjà en cours"}), 409
    return jsonify({"ok": True, "started": True}), 202


@app.post("/api/scan")
def api_scan():
    return start_scan_from_request(enqueue_files=bool(request.args.get("enqueue")))


@app.post("/api/queue")
def api_queue():
    return start_scan_from_request(enqueue_files=True)


@app.get("/api/scan")
def api_scan_status():
    with state_lock:
        return jsonify(dict(scan_state))


@app.get("/api/jobs")
def api_jobs():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 300").fetchall()
        counts = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status,COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()}
    with state_lock:
        active = active_job_id
    return jsonify({
        "jobs": [dict(row) for row in rows],
        "counts": counts, "paused": is_paused(), "active_job_id": active,
    })


@app.post("/api/pause")
def api_pause():
    set_paused(True)
    return jsonify({"ok": True})


@app.post("/api/resume")
def api_resume():
    set_paused(False)
    worker_wakeup.set()
    ensure_worker()
    return jsonify({"ok": True})


@app.post("/api/cancel/<job_id>")
def api_cancel(job_id: str):
    global cancel_job_id
    with connect() as conn:
        updated = conn.execute(
            "UPDATE jobs SET status='cancelled',stage='Annulé',updated_at=? WHERE id=? AND status='queued'",
            (now(), job_id),
        ).rowcount
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return jsonify({"error": "Job introuvable"}), 404
    if not updated and row["status"] == "running":
        with state_lock:
            if active_job_id == job_id:
                cancel_job_id = job_id
    return jsonify({"ok": True})


@app.get("/api/logs/<job_id>")
def api_logs(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        return jsonify({"error": "Identifiant invalide"}), 400
    path = LOG_DIR / f"{job_id}.log"
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 60000))
            return jsonify({"log": stream.read().decode("utf-8", errors="replace")[-30000:]})
    except OSError:
        return jsonify({"log": ""})


def ollama_get(base: str, path: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(base + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read(1_000_000).decode("utf-8"))


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
            payload = ollama_get(base, "/api/tags", 3)
            models = [item.get("name") for item in payload.get("models", []) if item.get("name")]
            try:
                version = ollama_get(base, "/api/version", 2).get("version", "inconnue")
            except (OSError, ValueError, urllib.error.URLError):
                version = "inconnue"
            return jsonify({"enabled": bool(settings.get("ollama_enabled")), "connected": True, "url": base,
                            "version": version, "models": models,
                            "selected_model": settings.get("ollama_model", "")})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{base}: {str(exc)[:160]}")
    return jsonify({"enabled": bool(settings.get("ollama_enabled")), "connected": False,
                    "url": configured, "models": [],
                    "message": "Ollama non joignable depuis le conteneur Uscale",
                    "details": errors})


@app.post("/api/ollama/test")
def api_ollama_test():
    try:
        payload = json_body()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    base = str(payload.get("url", get_settings().get("ollama_url", ""))).strip().rstrip("/")
    if not base.startswith(("http://", "https://")) or len(base) > 500:
        return jsonify({"ok": False, "error": "Adresse HTTP(S) invalide"}), 400
    try:
        data = ollama_get(base, "/api/tags", 5)
        version = ollama_get(base, "/api/version", 3).get("version", "inconnue")
        models = [item.get("name") for item in data.get("models", []) if item.get("name")]
        return jsonify({"ok": True, "url": base, "version": version, "models": models})
    except Exception:  # noqa: BLE001
        return jsonify({"ok": False, "error": "Ollama ne répond pas à cette adresse (API /api/tags)"}), 502


@app.post("/api/ollama/config")
def api_ollama_config():
    try:
        settings = save_ollama_settings(json_body())
        return jsonify({"ok": True, "enabled": settings["ollama_enabled"],
                        "url": settings["ollama_url"], "model": settings["ollama_model"]})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/health")
def api_health():
    docker = shutil.which("docker") is not None
    docker_ok = False
    docker_error = None
    image_present = None
    if docker:
        try:
            check = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                                   capture_output=True, text=True, timeout=8)
            docker_ok = check.returncode == 0
            if not docker_ok:
                docker_error = check.stderr.strip()[-500:]
            else:
                inspect = subprocess.run(["docker", "image", "inspect", VIDEO2X_IMAGE],
                                         capture_output=True, timeout=8)
                image_present = inspect.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            docker_error = f"Docker ne répond pas : {exc}"
    with state_lock:
        gpu = dict(gpu_state)
    return jsonify({
        "docker_cli": docker, "docker_ok": docker_ok, "error": docker_error,
        "image": VIDEO2X_IMAGE, "image_present": image_present, "gpu": gpu,
    })


@app.post("/api/health/gpu")
def api_health_gpu():
    started = start_gpu_check()
    return jsonify({"ok": True, "started": started}), 202


def main() -> None:
    from waitress import serve

    init_db()
    recover_interrupted_jobs()
    ensure_worker()
    serve(app, host="0.0.0.0", port=PORT, threads=8, ident="Uscale")


if __name__ == "__main__":
    main()
