"""Traitement d'une tâche : pipeline segmenté reprenable et aperçus multi-modèles.

Pipeline d'upscaling :
1. préparation : filtres (désentrelacement, débruitage), interpolation RIFE éventuelle, puis découpe en
   segments FFV1 intra à la résolution source ;
2. pour chaque segment : Video2X (Real-ESRGAN) -> intermédiaire sans perte -> encodage final du segment ;
   l'intermédiaire est supprimé aussitôt, l'espace disque reste borné à un segment ;
3. assemblage des segments encodés (copie) avec l'audio, les sous-titres et les chapitres de l'original.

Un segment encodé n'est jamais refait : une tâche interrompue (redémarrage, pause, plage horaire, erreur
puis « Relancer ») reprend au segment suivant.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, db, media, notify, ollama, runner
from . import settings as settings_mod
from .runner import JobCancelled, JobDeferred, JobError, Progress, check, human_size

# Fourni par le worker : renvoie une raison de suspendre la tâche entre deux segments, ou None.
defer_reason: Callable[[], str | None] = lambda: None  # noqa: E731

# Bitrate prudent utilisé pour estimer la taille des segments encodés (octets/s).
ENCODED_BYTES_PER_SECOND = 1_500_000


def job_settings(job: sqlite3.Row) -> dict[str, Any]:
    if job["settings_json"]:
        result = dict(settings_mod.DEFAULT_SETTINGS)
        result.update(json.loads(job["settings_json"]))
        return settings_mod.normalize(result)
    return settings_mod.effective_settings(settings_mod.get_settings(), Path(job["source"]))


def upscale_work_dir(job: sqlite3.Row) -> Path:
    return Path(job["destination"]).parent / f".uscale-{job['id']}"


def preview_work_dir(job_id: str, settings: dict[str, Any]) -> Path:
    # Sous la racine de données : le conteneur Video2X doit pouvoir y lire et écrire.
    return Path(settings["destination"]) / f".uscale-preview-{job_id}"


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def run_job(job: sqlite3.Row) -> None:
    job_id = job["id"]
    settings = job_settings(job)
    db.log_line(job_id, f"[{db.now()}] Début : {job['source']}")
    try:
        if job["kind"] == "preview":
            run_preview(job, settings)
        else:
            run_upscale(job, settings)
    except JobDeferred as exc:
        db.set_job(job_id, status="queued", stage=str(exc), eta_seconds=None)
        db.log_line(job_id, f"[{db.now()}] Suspendu : {exc} (reprise au segment suivant)")
    except JobCancelled as exc:
        cleanup_job_files(job)
        db.set_job(job_id, status="cancelled", stage="Arrêté", error=str(exc), eta_seconds=None)
        db.log_line(job_id, f"[{db.now()}] {exc}")
    except Exception as exc:  # noqa: BLE001 - toute erreur doit marquer la tâche en échec
        db.set_job(job_id, status="failed", stage="Erreur", error=str(exc), eta_seconds=None)
        db.log_line(job_id, f"[{db.now()}] ERREUR : {exc}")
        if job["kind"] != "preview":
            notify.notify_job(settings_mod.get_settings(), "failed", dict(job), str(exc))
    finally:
        runner.clear_cancel(job_id)


def cleanup_job_files(job: sqlite3.Row) -> None:
    """Supprime les fichiers de travail d'une tâche (annulation, suppression)."""
    if job["kind"] == "preview":
        settings = job_settings(job)
        remove_tree(preview_work_dir(job["id"], settings))
        return
    remove_tree(upscale_work_dir(job))
    destination = Path(job["destination"])
    destination.with_name(destination.stem + f".{job['id']}.partial.mkv").unlink(missing_ok=True)


def resolve_model(job: sqlite3.Row, settings: dict[str, Any], source: Path, info: dict[str, Any]) -> str:
    model = settings.get("model", settings_mod.LIVE_ACTION_MODEL)
    if model != settings_mod.AUTO_MODEL:
        return model
    if job["resolved_model"]:
        return job["resolved_model"]
    db.set_job(job["id"], stage="Choix du modèle (Ollama)")
    live = settings_mod.get_settings()
    chosen, reason = ollama.classify({**live, **{k: v for k, v in settings.items() if k.startswith("ollama_")}},
                                     source, info["duration"])
    db.log_line(job["id"], f"[{db.now()}] {reason} -> {chosen}")
    db.set_job(job["id"], resolved_model=chosen)
    return chosen


def prepare_work_dir(work: Path, fingerprint: dict[str, Any]) -> None:
    """Réutilise le dossier de travail seulement s'il a été produit avec exactement les mêmes paramètres."""
    fingerprint = json.loads(json.dumps(fingerprint))  # tuples -> listes, comme après relecture
    marker = work / "fingerprint.json"
    if work.exists():
        try:
            same = json.loads(marker.read_text(encoding="utf-8")) == fingerprint
        except (OSError, ValueError):
            same = False
        if not same:
            remove_tree(work)
    (work / "src").mkdir(parents=True, exist_ok=True)
    (work / "enc").mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(fingerprint), encoding="utf-8")


def chown_tree(path: Path, stop: Path) -> None:
    """Rend à l'utilisateur du NAS le fichier produit et les dossiers créés par Uscale."""
    runner.chown(path)
    for parent in path.parents:
        if parent == stop.parent or parent == config.DATA_ROOT or config.DATA_ROOT not in parent.parents:
            break
        try:
            if parent.stat().st_uid == 0:
                runner.chown(parent)
        except OSError:
            break


def run_upscale(job: sqlite3.Row, settings: dict[str, Any]) -> None:
    job_id = job["id"]
    source = Path(job["source"])
    destination = Path(job["destination"])
    work = upscale_work_dir(job)
    temp_final = destination.with_name(destination.stem + f".{job_id}.partial.mkv")
    disk_dir = destination.parent
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_final.unlink(missing_ok=True)
    if destination.exists():
        raise JobError(f"La destination existe déjà, elle n'est pas écrasée : {destination}")

    info = media.probe_source(source)
    model = resolve_model(job, settings, source, info)
    output_height = int(settings["output_height"])
    size = media.output_size(info, output_height)
    scale = media.choose_scale(model, info["height"], output_height)
    multiplier = int(settings.get("interpolate", 1))
    filters = media.prefilters(settings, info)
    fingerprint = {
        "source": str(source), "size": job["source_size"], "mtime": job["source_mtime_ns"],
        "model": model, "scale": scale, "filters": filters, "interpolate": multiplier, "output": size,
        "codec": settings.get("codec"), "crf": settings.get("crf"), "preset": settings.get("preset"),
        "segment_seconds": config.SEGMENT_SECONDS,
    }
    prepare_work_dir(work, fingerprint)
    segmented = (work / "segments.json").exists()
    progress = Progress(job_id, start=float(job["progress"] or 0) if segmented else 0.0)

    frames = info["frames"] * multiplier
    segment_frames = min(frames, int(config.SEGMENT_SECONDS * info["fps"] * multiplier) + 1)
    needed = media.estimate_ffv1_bytes(info["width"] * scale, info["height"] * scale, segment_frames)
    needed += int(info["duration"] * ENCODED_BYTES_PER_SECOND)
    if not segmented:
        needed += media.estimate_ffv1_bytes(info["width"], info["height"], frames)
        if multiplier > 1:
            needed += media.estimate_ffv1_bytes(info["width"], info["height"], info["frames"] + frames)
    free = shutil.disk_usage(disk_dir).free
    if free - needed < config.MIN_FREE_BYTES:
        raise JobError(f"Espace disque insuffisant : environ {human_size(needed)} nécessaires, "
                       f"{human_size(free)} libres (marge de {human_size(config.MIN_FREE_BYTES)} conservée)")
    db.log_line(job_id, f"[{db.now()}] Source {info['width']}×{info['height']} (SAR {info['sar']:.3f}, "
                        f"{info['field_order']}, {info['fps']:.3f} i/s) -> {size[0]}×{size[1]}, {model} ×{scale}, "
                        f"filtres [{', '.join(filters) or 'aucun'}], interpolation ×{multiplier}, "
                        f"encodeur {settings.get('codec')}, espace estimé {human_size(needed)}")
    runner.ensure_image(job_id)

    prep_share = 0.15 if multiplier > 1 else 0.05
    if not segmented:
        prepare_segments(job_id, source, work, info, filters, multiplier, progress, prep_share, disk_dir)
    segments = json.loads((work / "segments.json").read_text(encoding="utf-8"))["segments"]
    total = len(segments)
    db.set_job(job_id, segments_total=total)

    span = 0.97 - prep_share
    for index, name in enumerate(segments):
        encoded = work / "enc" / name
        if encoded.exists():
            continue
        reason = defer_reason()
        if reason:
            raise JobDeferred(reason)
        base = prep_share + span * index / total
        width = span / total
        db.set_job(job_id, stage=f"Upscaling IA ×{scale} · segment {index + 1}/{total}", segments_done=index)
        segment = work / "src" / name
        ai = work / f"ai_{name}"
        ai.unlink(missing_ok=True)
        check(runner.run_video2x(job_id, runner.upscale_args(segment, ai, model, scale),
                                 on_progress=progress.span(base, width * 0.85), disk_dir=disk_dir),
              f"Video2X a échoué sur le segment {index + 1}")
        if not ai.exists() or ai.stat().st_size == 0:
            raise JobError(f"Video2X n'a pas produit le segment {index + 1}")
        temp = work / "enc" / f"tmp_{name}"
        check(runner.run_process(job_id, media.segment_encode_command(ai, temp, size, settings), disk_dir=disk_dir),
              f"L'encodage du segment {index + 1} a échoué")
        temp.replace(encoded)
        ai.unlink(missing_ok=True)
        segment.unlink(missing_ok=True)
        db.set_job(job_id, segments_done=index + 1)
        progress.update(base + width, force=True)

    db.set_job(job_id, stage="Assemblage MKV / audio, sous-titres, chapitres", eta_seconds=None)
    concat = work / "concat.txt"
    media.write_concat_list(concat, [work / "enc" / name for name in segments])
    command, skipped = media.final_mux_command(concat, source, temp_final, info)
    if skipped:
        db.log_line(job_id, f"[{db.now()}] Sous-titres non compatibles MKV ignorés : {', '.join(skipped)}")
    check(runner.run_process(job_id, command, parse_progress=media_progress(info["duration"]),
                             on_progress=progress.span(0.97, 0.03), disk_dir=disk_dir),
          "FFmpeg a échoué pendant l'assemblage final")
    if not temp_final.exists() or temp_final.stat().st_size == 0:
        raise JobError("Le fichier MKV final est vide")
    problems = media.verify_output(temp_final, info, len(info["subtitles"]) - len(skipped), multiplier)
    if destination.exists():
        raise JobError(f"La destination a été créée pendant le traitement, elle n'est pas écrasée : {destination}")
    temp_final.replace(destination)
    chown_tree(destination, Path(settings.get("destination", destination.parent)))
    remove_tree(work)
    warning = ("Vérification : " + "; ".join(problems)) if problems else None
    if warning:
        db.log_line(job_id, f"[{db.now()}] ATTENTION {warning}")
    db.set_job(job_id, status="completed", stage="Terminé", progress=1.0, eta_seconds=None, warning=warning,
               segments_done=total)
    db.log_line(job_id, f"[{db.now()}] Terminé : {destination}")
    live = settings_mod.get_settings()
    notify.notify_job(live, "completed", dict(job), warning or "")
    notify.refresh_async(live)


def media_progress(duration: float) -> Callable[[str], float | None]:
    return runner.ffmpeg_progress_parser(duration)


def prepare_segments(job_id: str, source: Path, work: Path, info: dict[str, Any], filters: list[str],
                     multiplier: int, progress: Progress, share: float, disk_dir: Path) -> None:
    for old in (work / "src").glob("*"):
        old.unlink(missing_ok=True)
    segment_input = source
    segment_filters = filters
    if multiplier > 1:
        # Toujours passer par un FFV1 intra : Video2X peut perdre des images en lisant du H.264 directement.
        db.set_job(job_id, stage="Préparation (filtres, FFV1 sans perte)")
        prepped = work / "prep.mkv"
        check(runner.run_process(job_id, media.prefilter_command(source, prepped, filters, media.video_stream(info)),
                                 parse_progress=media_progress(info["duration"]),
                                 on_progress=progress.span(0, share * 0.2), disk_dir=disk_dir),
              "La préparation a échoué")
        segment_input = prepped
        segment_filters = []
        db.set_job(job_id, stage=f"Interpolation RIFE ×{multiplier}")
        interpolated = work / "rife.mkv"
        check(runner.run_video2x(job_id, runner.interpolate_args(segment_input, interpolated, multiplier),
                                 on_progress=progress.span(share * 0.2, share * 0.6), disk_dir=disk_dir),
              "L'interpolation RIFE a échoué")
        (work / "prep.mkv").unlink(missing_ok=True)
        segment_input = interpolated
    db.set_job(job_id, stage="Découpe en segments")
    cut_start, cut_width = (share * 0.8, share * 0.2) if multiplier > 1 else (0.0, share)
    # Après interpolation, l'entrée ne contient plus qu'un flux vidéo.
    stream = media.video_stream(info) if segment_input == source else "0:v:0"
    check(runner.run_process(job_id, media.segment_command(segment_input, work / "src" / "seg_%05d.mkv",
                                                           segment_filters, config.SEGMENT_SECONDS, stream),
                             parse_progress=media_progress(info["duration"]),
                             on_progress=progress.span(cut_start, cut_width), disk_dir=disk_dir),
          "La découpe en segments a échoué")
    (work / "rife.mkv").unlink(missing_ok=True)
    segments = sorted(p.name for p in (work / "src").glob("seg_*.mkv"))
    if not segments:
        raise JobError("Aucun segment produit")
    (work / "segments.json").write_text(json.dumps({"segments": segments}), encoding="utf-8")
    progress.update(share, force=True)


# ---------------------------------------------------------------- aperçus

def run_preview(job: sqlite3.Row, settings: dict[str, Any]) -> None:
    job_id = job["id"]
    params = settings.get("preview", {})
    source = Path(job["source"])
    out_dir = config.preview_dir() / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    work = preview_work_dir(job_id, settings)
    remove_tree(work)
    work.mkdir(parents=True)
    progress = Progress(job_id)
    try:
        info = media.probe_source(source)
        duration = float(params.get("duration", 10))
        start = max(0.0, min(float(params.get("start", 0)), max(0.0, info["duration"] - duration)))
        size = media.output_size(info, int(settings["output_height"]))
        filters = media.prefilters(settings, info)
        models = [m for m in params.get("models", []) if m in settings_mod.MODEL_SCALES] or [settings_mod.LIVE_ACTION_MODEL]
        manifest: dict[str, Any] = {
            "id": job_id, "source": str(source), "name": source.name, "start": start, "duration": duration,
            "width": size[0], "height": size[1], "filters": filters, "variants": [], "complete": False,
            "created_at": db.now(),
        }
        runner.ensure_image(job_id)
        db.set_job(job_id, stage="Extraction de l'extrait")
        clip = work / "clip.mkv"
        check(runner.run_process(job_id, media.clip_command(source, clip, start, duration, filters,
                                                            media.video_stream(info))),
              "Extraction de l'extrait impossible")
        steps = len(models) + 1
        check(runner.run_process(job_id, media.preview_encode_command(clip, out_dir / "original.mp4", size)),
              "Encodage de la référence impossible")
        manifest["variants"].append({"id": "original", "label": "Original agrandi (Lanczos, sans IA)",
                                     "file": "original.mp4"})
        write_manifest(out_dir, manifest)
        progress.update(1 / steps, force=True)
        for index, model in enumerate(models, start=1):
            scale = media.choose_scale(model, info["height"], size[1])
            db.set_job(job_id, stage=f"Aperçu {settings_mod.MODEL_LABELS.get(model, model)} ×{scale} ({index}/{len(models)})")
            ai = work / f"{model}.mkv"
            check(runner.run_video2x(job_id, runner.upscale_args(clip, ai, model, scale),
                                     on_progress=progress.span(index / steps, 0.9 / steps)),
                  f"Video2X a échoué pour {model}")
            check(runner.run_process(job_id, media.preview_encode_command(ai, out_dir / f"{model}.mp4", size)),
                  f"Encodage de l'aperçu {model} impossible")
            ai.unlink(missing_ok=True)
            manifest["variants"].append({"id": model, "label": f"{settings_mod.MODEL_LABELS.get(model, model)} (×{scale})",
                                         "file": f"{model}.mp4"})
            write_manifest(out_dir, manifest)
            progress.update((index + 1) / steps, force=True)
        manifest["complete"] = True
        write_manifest(out_dir, manifest)
        db.set_job(job_id, status="completed", stage="Aperçu prêt", progress=1.0, eta_seconds=None)
        db.log_line(job_id, f"[{db.now()}] Aperçu prêt : {out_dir}")
    finally:
        remove_tree(work)


def write_manifest(folder: Path, manifest: dict[str, Any]) -> None:
    temp = folder / "manifest.json.tmp"
    temp.write_text(json.dumps(manifest), encoding="utf-8")
    temp.replace(folder / "manifest.json")
