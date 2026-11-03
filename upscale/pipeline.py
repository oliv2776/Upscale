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

from . import config, db, media, models, notify, ollama, runner
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
    folder = Path(job["destination"]).parent
    work = folder / f".upscale-{job['id']}"
    legacy = folder / f".uscale-{job['id']}"  # tâche interrompue avec une version précédente
    if legacy.is_dir() and not work.exists():
        try:
            legacy.rename(work)
        except OSError:
            pass
    return work


def preview_work_dir(job_id: str, settings: dict[str, Any]) -> Path:
    # Sous la racine de données : le conteneur Video2X doit pouvoir y lire et écrire.
    return Path(settings["destination"]) / f".upscale-preview-{job_id}"


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


def chown_tree(path: Path, stop: Path, owner: runner.Owner | None) -> None:
    """Rend à l'utilisateur le fichier produit et les dossiers créés par Upscale (jusqu'à la destination)."""
    runner.chown(path, owner)
    for parent in path.parents:
        root = config.root_of(parent)
        if parent == stop.parent or root is None or parent == root:
            break
        try:
            if parent.stat().st_uid == 0:
                runner.chown(parent, owner)
        except OSError:
            break


def output_owner(source: Path, settings: dict[str, Any], destination: Path) -> runner.Owner | None:
    root = Path(settings.get("destination", destination.parent))
    return runner.default_owner(source, root, settings_mod.source_root_for(source, settings))


def run_upscale(job: sqlite3.Row, settings: dict[str, Any]) -> None:
    job_id = job["id"]
    source = Path(job["source"])
    destination = Path(job["destination"])
    work = upscale_work_dir(job)
    temp_final = destination.with_name(destination.stem + f".{job_id}.partial.mkv")
    disk_dir = destination.parent
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_final.unlink(missing_ok=True)
    replace = bool(settings.get("replace_original"))
    in_place = replace and destination == source  # film.mkv remplacé par film.mkv
    if destination.exists() and not in_place:
        raise JobError(f"La destination existe déjà, elle n'est pas écrasée : {destination}")

    info = media.probe_source(source)
    model = resolve_model(job, settings, source, info)
    output_height = int(settings["output_height"])
    size = media.output_size(info, output_height)
    scale = media.choose_scale(model, info["height"], output_height)
    multiplier = int(settings.get("interpolate", 1))
    filters = media.prefilters(settings, info)
    custom = models.is_custom(model)
    if custom and not models.get(model):
        raise JobError(f"Le modèle personnalisé {model} a été supprimé : choisis un autre modèle puis « Relancer »")
    # Le moteur des modèles personnalisés travaille en images PNG : segments plus courts.
    segment_seconds = min(config.SEGMENT_SECONDS, config.CUSTOM_SEGMENT_SECONDS) if custom else config.SEGMENT_SECONDS
    fingerprint = {
        "source": str(source), "size": job["source_size"], "mtime": job["source_mtime_ns"],
        "model": model, "scale": scale, "filters": filters, "interpolate": multiplier, "output": size,
        "codec": settings.get("codec"), "crf": settings.get("crf"), "preset": settings.get("preset"),
        "render": settings_mod.render_params(settings),
        "segment_seconds": segment_seconds,
    }
    prepare_work_dir(work, fingerprint)
    segmented = (work / "segments.json").exists()
    progress = Progress(job_id, start=float(job["progress"] or 0) if segmented else 0.0)

    frames = info["frames"] * multiplier
    segment_frames = min(frames, int(segment_seconds * info["fps"] * multiplier) + 1)
    needed = media.estimate_ffv1_bytes(info["width"] * scale, info["height"] * scale, segment_frames)
    if custom:  # images PNG du segment (entrée + sortie agrandie), environ 1,5 octet par pixel
        needed += int(segment_frames * info["width"] * info["height"] * 1.5 * (1 + scale * scale))
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
                        f"encodeur {settings.get('codec')}, rendu {render_label(settings)}, "
                        f"espace estimé {human_size(needed)}")
    if not custom or multiplier > 1:  # Video2X sert aussi à l'interpolation RIFE
        runner.ensure_image(job_id)

    prep_share = 0.15 if multiplier > 1 else 0.05
    if not segmented:
        prepare_segments(job_id, source, work, info, filters, multiplier, progress, prep_share, disk_dir,
                         segment_seconds)
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
        upscale_video(job_id, model, segment, ai, scale, info["fps"] * multiplier,
                      on_progress=progress.span(base, width * 0.85), disk_dir=disk_dir)
        temp = work / "enc" / f"tmp_{name}"
        check(runner.run_process(job_id, media.segment_encode_command(ai, temp, size, settings, original=segment),
                                 disk_dir=disk_dir),
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
    if destination.exists() and not in_place:
        raise JobError(f"La destination a été créée pendant le traitement, elle n'est pas écrasée : {destination}")
    owner = output_owner(source, settings, destination)  # avant de déplacer l'original
    warning = ("Vérification : " + "; ".join(problems)) if problems else None
    if replace:
        destination, warning = place_replacing_original(job_id, source, destination, temp_final, settings, warning)
        stop = settings_mod.source_root_for(source, settings)
    else:
        temp_final.replace(destination)
        stop = Path(settings.get("destination", destination.parent))
    chown_tree(destination, stop, owner)
    remove_tree(work)
    if warning:
        db.log_line(job_id, f"[{db.now()}] ATTENTION {warning}")
    db.set_job(job_id, status="completed", stage="Terminé", progress=1.0, eta_seconds=None, warning=warning,
               segments_done=total, destination=str(destination))
    db.log_line(job_id, f"[{db.now()}] Terminé : {destination}")
    live = settings_mod.get_settings()
    notify.notify_job(live, "completed", dict(job), warning or "")
    notify.refresh_async(live)


def unique_path(path: Path) -> Path:
    candidate, index = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        index += 1
    return candidate


def trash_location(source: Path, settings: dict[str, Any]) -> Path:
    """Emplacement de l'original dans la corbeille : <dossier source>/.upscale-corbeille/<chemin relatif>."""
    root = settings_mod.source_root_for(source, settings)
    try:
        relative = source.relative_to(root)
    except ValueError:
        root, relative = source.parent, Path(source.name)
    return unique_path(root / settings_mod.TRASH_DIR / relative)


def place_replacing_original(job_id: str, source: Path, destination: Path, temp_final: Path,
                             settings: dict[str, Any], warning: str | None) -> tuple[Path, str | None]:
    """Remplace l'original par la nouvelle version. L'original va dans la corbeille (ou est supprimé).

    Si la vérification a relevé un problème, rien n'est remplacé : la nouvelle version est posée à côté.
    """
    if warning:
        alternative = unique_path(source.with_name(f"{source.stem} - Upscale.mkv"))
        temp_final.replace(alternative)
        db.log_line(job_id, f"[{db.now()}] Original conservé (vérification en échec), nouvelle version : {alternative}")
        return alternative, f"{warning} — original conservé, nouvelle version à côté : {alternative.name}"
    trash = trash_location(source, settings)
    trash.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(trash))
    try:
        temp_final.replace(destination)
    except OSError:
        shutil.move(str(trash), str(source))  # on remet l'original en place
        raise
    days = int(settings.get("trash_days", 30))
    if days == 0:
        trash.unlink(missing_ok=True)
        db.log_line(job_id, f"[{db.now()}] Original remplacé et supprimé : {source}")
        db.set_job(job_id, trash_path=None, trashed_at=db.now())
    else:
        db.log_line(job_id, f"[{db.now()}] Original remplacé, conservé {days} jours dans la corbeille : {trash}")
        db.set_job(job_id, trash_path=str(trash), trashed_at=db.now())
    return destination, None


def render_label(settings: dict[str, Any]) -> str:
    strength, grain = settings_mod.render_params(settings)
    name = settings_mod.RENDER_LABELS.get(settings.get("render", "ai"), "IA pure")
    return f"{name} ({strength} % IA, grain {grain})"


def media_progress(duration: float) -> Callable[[str], float | None]:
    return runner.ffmpeg_progress_parser(duration)


def prepare_segments(job_id: str, source: Path, work: Path, info: dict[str, Any], filters: list[str],
                     multiplier: int, progress: Progress, share: float, disk_dir: Path,
                     segment_seconds: int) -> None:
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
                                                           segment_filters, segment_seconds, stream),
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

def upscale_video(job_id: str, model: str, source: Path, output: Path, scale: int, fps: float, *,
                  on_progress: Callable[[float], None] | None = None, disk_dir: Path | None = None) -> None:
    """Agrandit une vidéo sans perte avec le bon moteur : Video2X (modèles intégrés) ou ncnn (personnalisés)."""
    output.unlink(missing_ok=True)
    if models.is_custom(model):
        meta = models.get(model)
        if not meta:
            raise JobError(f"Le modèle personnalisé {model} a été supprimé")
        runner.run_custom_upscale(job_id, source, output, meta, fps, on_progress=on_progress, disk_dir=disk_dir)
    else:
        check(runner.run_video2x(job_id, runner.upscale_args(source, output, model, scale),
                                 on_progress=on_progress, disk_dir=disk_dir),
              f"Video2X a échoué ({model})")
    if not output.exists() or output.stat().st_size == 0:
        raise JobError(f"Aucune image agrandie produite ({model})")


def preview_model(settings: dict[str, Any], source: Path, info: dict[str, Any]) -> str:
    model = settings.get("model", settings_mod.LIVE_ACTION_MODEL)
    if model == settings_mod.AUTO_MODEL:
        model, _ = ollama.classify({**settings_mod.get_settings(), **{k: v for k, v in settings.items()
                                                                     if k.startswith("ollama_")}},
                                   source, info["duration"])
    return model


def run_preview(job: sqlite3.Row, settings: dict[str, Any]) -> None:
    """Aperçu d'un extrait : original agrandi, « Votre configuration » (exactement comme le fichier final),
    puis chaque modèle coché, en IA pure et en « Peau normale »."""
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
        available = settings_mod.available_models()
        checked = [m for m in params.get("models", []) if m in available]
        own_model = preview_model(settings, source, info)
        to_run = list(dict.fromkeys([own_model, *checked]))
        manifest: dict[str, Any] = {
            "id": job_id, "source": str(source), "name": source.name, "start": start, "duration": duration,
            "width": size[0], "height": size[1], "filters": filters, "variants": [], "complete": False,
            "created_at": db.now(),
        }
        if any(not models.is_custom(m) for m in to_run):
            runner.ensure_image(job_id)
        db.set_job(job_id, stage="Extraction de l'extrait")
        clip = work / "clip.mkv"
        check(runner.run_process(job_id, media.clip_command(source, clip, start, duration, filters,
                                                            media.video_stream(info))),
              "Extraction de l'extrait impossible")
        steps = len(to_run) + 1
        check(runner.run_process(job_id, media.preview_encode_command(clip, out_dir / "original.mp4", size)),
              "Encodage de la référence impossible")
        manifest["variants"].append({"id": "original", "label": "Original agrandi (Lanczos, sans IA)",
                                     "file": "original.mp4"})
        write_manifest(out_dir, manifest)
        progress.update(1 / steps, force=True)

        def add(variant_id: str, label: str) -> None:
            manifest["variants"].append({"id": variant_id, "label": label, "file": f"{variant_id}.mp4"})
            write_manifest(out_dir, manifest)

        for index, model in enumerate(to_run, start=1):
            scale = media.choose_scale(model, info["height"], size[1])
            name = settings_mod.model_label(model)
            db.set_job(job_id, stage=f"Aperçu {name} ×{scale} ({index}/{len(to_run)})")
            ai = work / f"{model}.mkv"
            upscale_video(job_id, model, clip, ai, scale, info["fps"],
                          on_progress=progress.span(index / steps, 0.9 / steps))
            if model == own_model:
                # Exactement la chaîne du fichier final : modèle, rendu, encodeur et qualité (CRF).
                final = work / "config.mkv"
                check(runner.run_process(job_id, media.segment_encode_command(ai, final, size, settings, original=clip)),
                      "Encodage « Votre configuration » impossible")
                check(runner.run_process(job_id, media.preview_encode_command(final, out_dir / "config.mp4", size)),
                      "Encodage « Votre configuration » impossible")
                add("config", f"Votre configuration · {name} · {render_label(settings)} · "
                              f"{settings.get('codec', 'hevc').upper()} CRF {settings.get('crf')}")
            if model in checked:
                check(runner.run_process(job_id, media.preview_encode_command(ai, out_dir / f"{model}.mp4", size)),
                      f"Encodage de l'aperçu {model} impossible")
                add(model, f"{name} (×{scale})")
                if params.get("peau_normale", True):
                    # Même sortie IA, mélangée à l'original : coûte un simple encodage de plus.
                    strength, grain = settings_mod.RENDER_PRESETS["peau-normale"]
                    check(runner.run_process(job_id, media.preview_encode_command(
                        ai, out_dir / f"{model}-peau-normale.mp4", size, original=clip, strength=strength, grain=grain)),
                        f"Encodage de l'aperçu Peau normale ({model}) impossible")
                    add(f"{model}-peau-normale", f"{name} (×{scale}) · Peau normale")
            ai.unlink(missing_ok=True)
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
