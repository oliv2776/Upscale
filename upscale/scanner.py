"""Analyse de la bibliothèque et mise en file (réglages figés par tâche)."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import config, db, media
from . import settings as settings_mod

scan_state: dict[str, Any] = {"running": False}
_lock = threading.Lock()
worker_wakeup = threading.Event()


HIDDEN_PREFIXES = (".upscale", ".uscale")


def library_id(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]


def is_upscale_internal(path: Path, root: Path) -> bool:
    """Dossiers de travail, aperçus et corbeille d'Upscale : jamais analysés."""
    return any(part.startswith(HIDDEN_PREFIXES) for part in path.relative_to(root).parts)


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def list_candidates(source_root: Path, recursive: bool, exclude: Path | None = None) -> list[Path]:
    """Parcourt récursivement sans échouer sur un dossier illisible ou un lien symbolique.

    ``exclude`` (la destination) est ignoré : on peut ainsi choisir un dossier parent qui contient
    la destination sans rescanner les sorties.
    """
    found: list[Path] = []
    excluded = exclude.resolve() if exclude is not None else None

    def onerror(_error: OSError) -> None:  # dossier illisible : on continue l'analyse
        return

    for dirpath, dirnames, filenames in os.walk(source_root, topdown=True, onerror=onerror,
                                                  followlinks=False):
        current = Path(dirpath)
        if not recursive:
            dirnames[:] = []
        if excluded is not None and _inside(current, excluded):
            dirnames[:] = []
            continue
        dirnames[:] = [
            name for name in dirnames
            if not name.startswith(HIDDEN_PREFIXES)
            and not (current / name).is_symlink()
            and not (excluded is not None and _inside(current / name, excluded))
        ]
        for name in filenames:
            path = current / name
            try:
                if path.is_symlink() or path.suffix.lower() not in media.VIDEO_EXTENSIONS or not path.is_file():
                    continue
                if is_upscale_internal(path, source_root):
                    continue
                if source_root not in path.parents:
                    continue
                found.append(path)
            except OSError:
                continue
    found.sort()
    return found


def cached_dimensions(conn: sqlite3.Connection, path: Path, stat: os.stat_result) -> tuple[int, int] | None:
    row = conn.execute("SELECT size,mtime_ns,width,height FROM probe_cache WHERE path=?", (str(path),)).fetchone()
    if row and row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
        return (row["width"], row["height"]) if row["width"] else None
    dims = media.probe_dimensions(path)
    conn.execute("""INSERT OR REPLACE INTO probe_cache(path,size,mtime_ns,width,height,updated_at)
                    VALUES(?,?,?,?,?,?)""",
                 (str(path), stat.st_size, stat.st_mtime_ns,
                  dims[0] if dims else None, dims[1] if dims else None, db.now()))
    return dims


def source_labels(sources: list[Path]) -> dict[Path, str]:
    """Nom du sous-dossier de destination de chaque source (seulement s'il y a plusieurs sources)."""
    labels: dict[Path, str] = {}
    used: set[str] = set()
    for source in sources:
        base = source.name or "data"
        label, index = base, 2
        while label.lower() in used:
            label, index = f"{base}-{index}", index + 1
        used.add(label.lower())
        labels[source] = label
    return labels


def scan_files(settings: dict[str, Any], progress: Callable[[int, int], None] | None = None,
               min_age_seconds: float = 0,
               on_probe: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    sources, destination_root = settings_mod.validate_folders(settings)
    labels = source_labels(sources)
    candidates = [(source, path) for source in sources
                  for path in list_candidates(source, settings.get("include_subfolders", True),
                                              destination_root)]
    stems = Counter((p.parent, p.stem.lower()) for _, p in candidates)
    suffix = settings.get("output_suffix", "")
    replace = bool(settings.get("replace_original"))
    max_input = int(settings["max_input_height"])
    files: list[dict[str, Any]] = []
    resolutions = {"under_360": 0, "360_480": 0, "over_480": 0, "unknown": 0}
    already_known = too_recent = 0
    with db.connect() as conn:
        known = {}
        for row in conn.execute("SELECT source,source_size,source_mtime_ns,status,destination,settings_json "
                                "FROM jobs WHERE kind='upscale'"):
            snapshot = json.loads(row["settings_json"] or "{}")
            known[(row["source"], row["source_size"], row["source_mtime_ns"])] = (
                row["status"], row["destination"],
                (snapshot.get("destination"), snapshot.get("output_suffix", ""), bool(snapshot.get("replace_original"))))
        # Fichiers produits par Upscale : à ne jamais retraiter (indispensable en mode remplacement).
        produced = {destination for status, destination, _ in known.values() if status == "completed"}
        intent = (settings.get("destination"), suffix, replace)
        # Cache des dimensions : évite un ffprobe par fichier à chaque analyse.
        cache = {row["path"]: (row["size"], row["mtime_ns"], row["width"], row["height"])
                 for row in conn.execute("SELECT path,size,mtime_ns,width,height FROM probe_cache")}
        pending: list[dict[str, Any]] = []
        for index, (source_root, path) in enumerate(candidates, 1):
            if progress and (index % 10 == 0 or index == len(candidates)):
                progress(index, len(candidates))
            try:
                if str(path) in produced:
                    continue
                stat = path.stat()
                if min_age_seconds and time.time() - stat.st_mtime < min_age_seconds:
                    too_recent += 1  # fichier probablement en cours de copie
                    continue
                rel = path.relative_to(source_root)
                ambiguous = stems[(path.parent, path.stem.lower())] > 1
                if replace:
                    # Remplacement : la nouvelle version prend la place de l'original, dans son dossier.
                    output = path.parent / media.output_relative(Path(path.name), ambiguous, suffix).name
                    if output != path and output.exists():
                        continue  # un autre fichier occupe déjà ce nom
                else:
                    # Plusieurs sources : un sous-dossier par source dans la destination.
                    base = destination_root / labels[source_root] if len(sources) > 1 else destination_root
                    output = base / media.output_relative(rel, ambiguous, suffix)
                    if output.exists():
                        continue
                status, previous, previous_intent = known.get((str(path), stat.st_size, stat.st_mtime_ns),
                                                              (None, None, None))
                # Déjà traité (ou en cours) : ignoré. Une autre destination, un autre suffixe ou le mode
                # remplacement donnent une nouvelle sortie : le fichier est alors retraité. Un simple changement
                # d'organisation (ajout d'une 2e source) ne refait pas une vidéo dont la sortie existe toujours.
                if status in {"queued", "running"} or (status == "completed" and (
                        previous == str(output)
                        or (previous_intent == intent and previous and Path(previous).exists()))):
                    already_known += 1
                    continue
                item = {"source": str(path), "relative": rel.as_posix(), "destination": str(output),
                        "ambiguous": ambiguous, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                        "path": path}
                cached = cache.get(str(path))
                if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
                    item["dims"] = (cached[2], cached[3]) if cached[2] else None
                pending.append(item)
            except (OSError, ValueError):
                continue

        # Les ffprobe manquants sont lancés en parallèle : c'est la partie lente d'une première analyse.
        missing = [item for item in pending if "dims" not in item]
        if missing:
            workers = max(1, min(config.SCAN_WORKERS, len(missing)))
            if on_probe:
                on_probe(0, len(missing))
            done = 0
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="upscale-probe") as pool:
                futures = {pool.submit(media.probe_dimensions, item["path"]): item for item in missing}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        dims = future.result()
                    except Exception:  # noqa: BLE001 - un fichier illisible ne doit pas arrêter l'analyse
                        dims = None
                    item["dims"] = dims
                    conn.execute("""INSERT OR REPLACE INTO probe_cache(path,size,mtime_ns,width,height,updated_at)
                                    VALUES(?,?,?,?,?,?)""",
                                 (item["source"], item["size"], item["mtime_ns"],
                                  dims[0] if dims else None, dims[1] if dims else None, db.now()))
                    done += 1
                    if on_probe:
                        on_probe(done, len(missing))

        for item in pending:
            dims = item.get("dims")
            if not dims:
                resolutions["unknown"] += 1
                continue
            width, height = dims
            if height < 360:
                resolutions["under_360"] += 1
            elif height <= 480:
                resolutions["360_480"] += 1
            else:
                resolutions["over_480"] += 1
            if height > max_input or height < 120:
                continue
            files.append({"id": library_id(Path(item["source"])), "source": item["source"],
                          "relative": item["relative"], "destination": item["destination"],
                          "ambiguous": item.get("ambiguous", False),
                          "width": width, "height": height,
                          "size": item["size"], "mtime_ns": item["mtime_ns"]})
    return {"files": files, "already_known": already_known, "too_recent": too_recent,
            "checked": len(candidates), "resolutions": resolutions}


def next_position(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT MAX(position) AS p FROM jobs").fetchone()
    return max(float(row["p"] or 0) + 1, time.time())


def enqueue(files: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, int]:
    """Met les fichiers en file en figeant pour chacun les réglages effectifs (profil compris)."""
    added = conflicts = 0
    with db.connect() as conn:
        active = {row["destination"]: row["source"] for row in
                  conn.execute("SELECT source,destination FROM jobs WHERE status IN ('queued','running')")}
        for item in files:
            owner = active.get(item["destination"])
            if owner and owner != item["source"]:
                conflicts += 1
                continue
            snapshot = json.dumps(settings_mod.effective_settings(settings, Path(item["source"])))
            existing = conn.execute(
                "SELECT id,status,destination FROM jobs WHERE source=? AND source_size=? AND source_mtime_ns=?",
                (item["source"], item["size"], item["mtime_ns"]),
            ).fetchone()
            if existing:
                retry = existing["status"] in {"failed", "cancelled"}
                new_output = existing["status"] == "completed" and existing["destination"] != item["destination"]
                if retry or new_output:
                    conn.execute("""
                      UPDATE jobs SET status='queued',stage='En attente',error=NULL,warning=NULL,progress=NULL,
                        eta_seconds=NULL,segments_done=NULL,segments_total=NULL,resolved_model=NULL,
                        destination=?,width=?,height=?,settings_json=?,position=?,updated_at=?
                      WHERE id=?
                    """, (item["destination"], item["width"], item["height"], snapshot, next_position(conn),
                          db.now(), existing["id"]))
                    added += 1
                    active[item["destination"]] = item["source"]
                continue
            conn.execute("""
              INSERT INTO jobs (id,source,destination,source_size,source_mtime_ns,width,height,status,stage,
                                created_at,updated_at,settings_json,position,kind,priority)
              VALUES(?,?,?,?,?,?,?,'queued','En attente',?,?,?,?,'upscale',0)
            """, (uuid.uuid4().hex[:12], item["source"], item["destination"], item["size"], item["mtime_ns"],
                  item["width"], item["height"], db.now(), db.now(), snapshot, next_position(conn)))
            active[item["destination"]] = item["source"]
            added += 1
    worker_wakeup.set()
    return {"added": added, "conflicts": conflicts}


def _run_scan(settings: dict[str, Any], enqueue_files: bool, min_age_seconds: float) -> None:
    def progress(done: int, total: int) -> None:
        with _lock:
            scan_state.update(checked=done, total=total)

    def probe(done: int, total: int) -> None:
        with _lock:
            scan_state.update(phase="Lecture des métadonnées", probe_done=done, probe_total=total)

    try:
        result = scan_files(settings, progress, min_age_seconds, probe)
        db.replace_library_items(result["files"])
        update: dict[str, Any] = {
            "found": len(result["files"]), "already_known": result["already_known"],
            "too_recent": result["too_recent"], "checked": result["checked"], "total": result["checked"],
            "recursive": bool(settings.get("include_subfolders", True)),
            "resolutions": result["resolutions"],
            "files": [f["relative"] for f in result["files"][:200]],
        }
        if enqueue_files:
            update.update(enqueue(result["files"], settings))
        with _lock:
            scan_state.update(update, running=False, phase=None, finished_at=db.now())
    except Exception as exc:  # noqa: BLE001
        with _lock:
            scan_state.update(running=False, error=str(exc), finished_at=db.now())


def start_scan(settings: dict[str, Any], enqueue_files: bool, *, min_age_seconds: float = 0,
               origin: str = "manual") -> bool:
    with _lock:
        if scan_state.get("running"):
            return False
        scan_state.clear()
        scan_state.update(running=True, enqueue=enqueue_files, checked=0, total=0, origin=origin,
                          started_at=db.now(), error=None, phase=None, probe_done=0, probe_total=0)
    threading.Thread(target=_run_scan, args=(settings, enqueue_files, min_age_seconds), daemon=True).start()
    return True


def get_scan_state() -> dict[str, Any]:
    with _lock:
        return dict(scan_state)
