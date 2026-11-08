"""File d'attente : réservation, plage horaire, dossier surveillé, maintenance et gestion des tâches."""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config, db, pipeline, runner, scanner
from . import settings as settings_mod

log = logging.getLogger("upscale")
_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()


# ---------------------------------------------------------------- plage horaire et pause

def _minutes(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def within_schedule(settings: dict[str, Any], moment: datetime | None = None) -> bool:
    if not settings.get("schedule_enabled"):
        return True
    moment = moment or datetime.now()
    current = moment.hour * 60 + moment.minute
    start, end = _minutes(settings["schedule_start"]), _minutes(settings["schedule_end"])
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def defer_reason() -> str | None:
    if db.is_paused():
        return "En pause"
    settings = settings_mod.get_settings()
    if not within_schedule(settings):
        return f"En attente de la plage horaire ({settings['schedule_start']}–{settings['schedule_end']})"
    return None


pipeline.defer_reason = defer_reason


# ---------------------------------------------------------------- worker

def claim_next_job() -> Any:
    """Réserve atomiquement la prochaine tâche : une annulation concurrente ne peut pas être écrasée.

    Les aperçus passent toujours ; les upscalings attendent la fin de la pause et la plage horaire.
    """
    allow_upscale = defer_reason() is None
    with db.connect() as conn:
        candidate = conn.execute(
            "SELECT * FROM jobs WHERE status='queued' AND (kind='preview' OR ?) "
            "ORDER BY priority DESC, position ASC LIMIT 1", (1 if allow_upscale else 0,),
        ).fetchone()
    if not candidate:
        return None
    runner.set_active(candidate["id"])
    with db.connect() as conn:
        claimed = conn.execute(
            "UPDATE jobs SET status='running',stage='Préparation',error=NULL,eta_seconds=NULL,updated_at=? "
            "WHERE id=? AND status='queued'", (db.now(), candidate["id"]),
        ).rowcount
    if not claimed:
        runner.set_active(None)
        return None
    return db.get_job(candidate["id"])


def worker_loop() -> None:
    while True:
        try:
            job = claim_next_job()
            if job:
                try:
                    pipeline.run_job(job)
                finally:
                    runner.set_active(None)
                continue
        except Exception as exc:  # noqa: BLE001 - le worker ne doit jamais mourir
            log.exception("Erreur du worker : %s", exc)
            time.sleep(5)
        scanner.worker_wakeup.wait(timeout=10)
        scanner.worker_wakeup.clear()


def _ensure_thread(name: str, target: Any) -> None:
    with _threads_lock:
        thread = _threads.get(name)
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=target, name=f"upscale-{name}", daemon=True)
            _threads[name] = thread
            thread.start()


def ensure_worker() -> None:
    _ensure_thread("worker", worker_loop)


def recover_interrupted_jobs() -> None:
    runner.cleanup_orphan_containers()
    with db.connect() as conn:
        conn.execute("""
          UPDATE jobs SET status='queued',stage='Reprise après redémarrage',eta_seconds=NULL,
            error='Le serveur a redémarré pendant ce traitement; la tâche reprendra au segment en cours.',
            updated_at=? WHERE status='running'
        """, (db.now(),))


# ---------------------------------------------------------------- dossier surveillé

def watch_tick(moment: float | None = None) -> bool:
    """Lance une analyse automatique si l'intervalle est écoulé. Renvoie True si une analyse démarre."""
    settings = settings_mod.get_settings()
    if not settings.get("watch_enabled"):
        return False
    moment = moment or time.time()
    last = float(db.get_state("watch_last", "0") or 0)
    if moment - last < int(settings["watch_interval"]) * 60:
        return False
    try:
        settings_mod.validate_folders(settings)
    except (ValueError, OSError) as exc:
        log.warning("Dossier surveillé invalide : %s", exc)
        return False
    if not scanner.start_scan(settings, True, min_age_seconds=120, origin="watch"):
        return False
    db.set_state("watch_last", str(moment))
    ensure_worker()
    return True


def watcher_loop() -> None:
    while True:
        try:
            watch_tick()
        except Exception as exc:  # noqa: BLE001
            log.exception("Erreur de la surveillance : %s", exc)
        time.sleep(30)


# ---------------------------------------------------------------- maintenance

def fix_ownership() -> int:
    """Rend à leur propriétaire les vidéos déjà produites qui appartiennent encore à root."""
    fixed = 0
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE kind='upscale' AND status='completed'").fetchall()
    for row in rows:
        destination = Path(row["destination"])
        try:
            if destination.stat().st_uid != 0:
                continue
        except OSError:
            continue
        settings = pipeline.job_settings(row)
        owner = pipeline.output_owner(Path(row["source"]), settings, destination)
        if owner and owner[0] not in (0, -1):
            pipeline.chown_tree(destination, Path(settings.get("destination", destination.parent)), owner)
            fixed += 1
    return fixed


def purge_trash(moment: float | None = None) -> int:
    """Supprime de la corbeille les originaux remplacés depuis plus de trash_days jours.

    L'ancienneté se lit sur la date de changement d'inode (ctime), mise à jour par le déplacement :
    la date de modification de la vidéo, elle, est conservée.
    """
    settings = settings_mod.get_settings()
    days = int(settings.get("trash_days", 30))
    moment = moment or time.time()
    roots = {Path(source) / settings_mod.TRASH_DIR for source in settings_mod.source_list(settings)}
    with db.connect() as conn:
        tracked = conn.execute("SELECT id,trash_path FROM jobs WHERE trash_path IS NOT NULL").fetchall()
    for row in tracked:
        parts = Path(row["trash_path"]).parts
        if settings_mod.TRASH_DIR in parts:
            roots.add(Path(*parts[: parts.index(settings_mod.TRASH_DIR) + 1]))
    removed = 0
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), reverse=True):
            try:
                if path.is_file() and moment - path.stat().st_ctime > days * 86400:
                    path.unlink()
                    removed += 1
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                continue
    with db.connect() as conn:
        for row in tracked:
            if not Path(row["trash_path"]).exists():
                conn.execute("UPDATE jobs SET trash_path=NULL WHERE id=?", (row["id"],))
    return removed


def maintenance() -> dict[str, int]:
    """Supprime les vieux journaux, le cache ffprobe obsolète, les anciens aperçus et la corbeille expirée ;
    corrige les propriétaires."""
    removed = {"logs": 0, "probe_cache": 0, "previews": 0, "owners": fix_ownership(), "trash": purge_trash()}
    cutoff = (datetime.now(UTC) - timedelta(days=config.LOG_RETENTION_DAYS)).isoformat()
    with db.connect() as conn:
        jobs = {row["id"]: row for row in conn.execute("SELECT id,status,updated_at FROM jobs")}
        for path in config.log_dir().glob("*.log"):
            row = jobs.get(path.stem)
            old = row is not None and row["status"] in {"completed", "cancelled"} and row["updated_at"] < cutoff
            if row is None or old:
                path.unlink(missing_ok=True)
                removed["logs"] += 1
        stale = [row["path"] for row in conn.execute("SELECT path FROM probe_cache LIMIT 20000")
                 if not Path(row["path"]).exists()]
        conn.executemany("DELETE FROM probe_cache WHERE path=?", [(p,) for p in stale])
        removed["probe_cache"] = len(stale)
        previews = conn.execute("SELECT id FROM jobs WHERE kind='preview' AND status IN ('completed','failed','cancelled') "
                                "ORDER BY updated_at DESC").fetchall()
        for row in previews[config.PREVIEW_KEEP:]:
            pipeline.remove_tree(config.preview_dir() / row["id"])
            (config.log_dir() / f"{row['id']}.log").unlink(missing_ok=True)
            conn.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
            removed["previews"] += 1
        known_previews = {row["id"] for row in conn.execute("SELECT id FROM jobs WHERE kind='preview'")}
    for folder in config.preview_dir().glob("*"):
        if folder.is_dir() and folder.name not in known_previews:
            pipeline.remove_tree(folder)
            removed["previews"] += 1
    return removed


def maintenance_loop() -> None:
    while True:
        try:
            result = maintenance()
            if any(result.values()):
                log.info("Maintenance : %s", result)
        except Exception as exc:  # noqa: BLE001
            log.exception("Erreur de maintenance : %s", exc)
        time.sleep(6 * 3600)


def start_background() -> None:
    ensure_worker()
    _ensure_thread("watcher", watcher_loop)
    _ensure_thread("maintenance", maintenance_loop)


# ---------------------------------------------------------------- gestion des tâches

JOB_ID_RE = re.compile(r"[0-9a-f]{12}")


def cancel(job_id: str) -> bool:
    """Annule une tâche en attente ou en cours ; False si elle n'existe pas.

    Lève ValueError si la tâche est déjà terminée : l'API répond alors 409 au lieu d'un « OK » trompeur.
    """
    with db.connect() as conn:
        updated = conn.execute(
            "UPDATE jobs SET status='cancelled',stage='Annulé',updated_at=? WHERE id=? AND status='queued'",
            (db.now(), job_id),
        ).rowcount
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return False
    if updated:
        pipeline.cleanup_job_files(row)
        return True
    if row["status"] == "running":
        if runner.request_cancel(job_id):
            return True
        raise ValueError("La tâche est en cours dans un autre processus : impossible de l'arrêter ici")
    raise ValueError("Cette tâche est déjà terminée")


def retry(job_id: str) -> None:
    """Remet en file une tâche en échec ou annulée ; les segments déjà encodés sont conservés."""
    with db.connect() as conn:
        updated = conn.execute(
            "UPDATE jobs SET status='queued',stage='En attente',error=NULL,eta_seconds=NULL,position=?,updated_at=? "
            "WHERE id=? AND status IN ('failed','cancelled')",
            (scanner.next_position(conn), db.now(), job_id),
        ).rowcount
    if not updated:
        raise ValueError("Seules les tâches en erreur ou annulées peuvent être relancées")
    scanner.worker_wakeup.set()


def delete(job_id: str) -> None:
    row = db.get_job(job_id)
    if not row:
        raise LookupError("Tâche introuvable")
    # Suppression conditionnelle : si le worker réserve la tâche entre-temps, on ne supprime pas ses fichiers.
    with db.connect() as conn:
        deleted = conn.execute("DELETE FROM jobs WHERE id=? AND status!='running'", (job_id,)).rowcount
    if not deleted:
        raise ValueError("Arrête la tâche avant de la supprimer")
    pipeline.cleanup_job_files(row)
    if row["kind"] == "preview":
        pipeline.remove_tree(config.preview_dir() / job_id)
    (config.log_dir() / f"{job_id}.log").unlink(missing_ok=True)
    with db.connect() as conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))


def clear(statuses: list[str]) -> int:
    allowed = [s for s in statuses if s in {"completed", "failed", "cancelled"}]
    if not allowed:
        raise ValueError("Statut invalide")
    with db.connect() as conn:
        rows = conn.execute(f"SELECT id FROM jobs WHERE kind='upscale' AND status IN ({','.join('?' * len(allowed))})",
                            allowed).fetchall()
    for row in rows:
        delete(row["id"])
    return len(rows)


def restore(job_id: str) -> None:
    """Annule un remplacement : supprime la version Upscale et remet l'original à sa place."""
    row = db.get_job(job_id)
    if not row:
        raise LookupError("Tâche introuvable")
    trash = Path(row["trash_path"]) if row["trash_path"] else None
    if not trash or not trash.exists():
        raise ValueError("L'original n'est plus dans la corbeille")
    original, produced = Path(row["source"]), Path(row["destination"])
    if original.exists() and original != produced:
        raise ValueError(f"Un fichier occupe déjà l'emplacement de l'original : {original}")
    original.parent.mkdir(parents=True, exist_ok=True)
    # Déplacement d'abord, suppression ensuite : un échec ne détruit pas la version produite.
    temp = original.with_name(f".{original.stem}.restore-{job_id}{original.suffix}")
    temp.unlink(missing_ok=True)

    def rollback() -> None:
        if temp.exists() and not trash.exists():
            try:
                shutil.move(str(temp), str(trash))
            except OSError:
                pass

    try:
        shutil.move(str(trash), str(temp))
        produced.unlink(missing_ok=True)
        temp.replace(original)
    except BaseException:
        rollback()
        raise
    db.set_job(job_id, trash_path=None, stage="Original restauré",
               warning="Original restauré, la version Upscale a été supprimée.")
    db.log_line(job_id, f"[{db.now()}] Original restauré : {original}")


def move(job_id: str, where: str) -> None:
    """Réordonne une tâche en attente : top, up, down, bottom."""
    with db.connect() as conn:
        queued = conn.execute("SELECT id,position FROM jobs WHERE status='queued' AND kind='upscale' "
                              "ORDER BY position ASC").fetchall()
        ids = [row["id"] for row in queued]
        if job_id not in ids:
            raise ValueError("Seules les tâches en attente peuvent être déplacées")
        index = ids.index(job_id)
        positions = [float(row["position"]) for row in queued]
        if where == "top":
            new = positions[0] - 1
        elif where == "bottom":
            new = positions[-1] + 1
        elif where in {"up", "down"}:
            other = index - 1 if where == "up" else index + 1
            if not 0 <= other < len(ids):
                return
            conn.execute("UPDATE jobs SET position=? WHERE id=?", (positions[index], ids[other]))
            new = positions[other]
        else:
            raise ValueError("Déplacement invalide")
        conn.execute("UPDATE jobs SET position=?,updated_at=? WHERE id=?", (new, db.now(), job_id))


def create_preview(source: Path, start: float, duration: float, models: list[str], peau_normale: bool = True) -> str:
    settings = settings_mod.get_settings()
    settings_mod.validate_folders(settings)
    snapshot = settings_mod.effective_settings(settings, source)
    snapshot["preview"] = {"start": start, "duration": duration, "models": models, "peau_normale": peau_normale}
    job_id = uuid.uuid4().hex[:12]
    with db.connect() as conn:
        # source_mtime_ns négatif et unique : un aperçu ne doit pas entrer en conflit avec la tâche du même fichier.
        conn.execute("""
          INSERT INTO jobs (id,source,destination,source_size,source_mtime_ns,status,stage,created_at,updated_at,
                            settings_json,position,kind,priority)
          VALUES(?,?,?,0,?,'queued','Aperçu en attente',?,?,?,?,'preview',100)
        """, (job_id, str(source), str(config.preview_dir() / job_id), -time.time_ns(), db.now(), db.now(),
              json.dumps(snapshot), scanner.next_position(conn)))
    scanner.worker_wakeup.set()
    ensure_worker()
    return job_id
