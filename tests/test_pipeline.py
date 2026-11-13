"""Traitement de bout en bout : segments, reprise, remplacement, aperçus."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from conftest import calls, make_video, needs_ffmpeg
from helpers import (
    _patch_rgb,
    add_disk,
    ffprobe_streams,
    insert_job,
    job_log,
    process_all,
    root_only,
    video_stream,
)

from upscale import config, db, media, pipeline, runner, scanner, worker
from upscale import settings as st
from upscale.web import app


def test_cut_pauses_when_segments_accumulate(env, tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    names = [f"seg_{index:05d}.mkv" for index in range(5)]
    state = {"done": 0, "pauses": 0, "resumes": 0, "paused": False}

    class FakeCutter:
        def __init__(self, job_id, command, **kwargs):
            (work / "segments.list").write_text("\n".join(names) + "\n")
            self.returncode = 0

        def running(self):
            return state["done"] < len(names)

        def paused(self):
            return state["paused"]

        def pause(self):
            state["pauses"] += 1
            state["paused"] = True

        def resume(self):
            state["resumes"] += 1
            state["paused"] = False

        def finish(self):
            pass

        def stop(self):
            pass

    def process(index, name, total, approximate, note=""):
        state["done"] += 1

    monkeypatch.setattr(pipeline.runner, "RunningProcess", FakeCutter)
    info = {"duration": 5.0, "width": 320, "height": 240, "sar": 1.0, "fps": 10.0, "video_index": 0}
    pipeline.cut_segments_while_upscaling("ffffffffffff", tmp_path / "source.mkv", work, info, [], 1, 5,
                                          tmp_path, process)
    assert state["done"] == len(names)
    assert state["pauses"] >= 1 and state["resumes"] >= 1


def test_running_process_can_be_paused(env):
    process = runner.RunningProcess("eeeeeeeeeeee", ["sleep", "30"])
    try:
        process.pause()
        assert process.paused() is True
        process.resume()
        assert process.paused() is False
    finally:
        process.stop()


def test_project_remaining_bytes():
    expected = int(1_000_000 * 80 * pipeline.SEGMENT_RATE_MARGIN)
    assert pipeline.project_remaining_bytes(1_000_000, 120, 40) == expected
    assert pipeline.project_remaining_bytes(0, 120, 120) == 0


def test_restore_failure_keeps_produced_file(env, monkeypatch):
    videos = env / "videos"
    produced = videos / "film.mkv"
    produced.write_bytes(b"upscale")
    trash = videos / ".upscale-corbeille" / "film.avi"
    trash.parent.mkdir(parents=True)
    trash.write_bytes(b"original")
    original = videos / "film.avi"
    with db.connect() as conn:
        conn.execute("""INSERT INTO jobs (id,source,destination,source_size,source_mtime_ns,status,stage,
                                          created_at,updated_at,trash_path)
                        VALUES('dddddddddddd',?,?,1,1,'completed','Terminé','2026-01-01','2026-01-01',?)""",
                     (str(original), str(produced), str(trash)))

    def fail(*args, **kwargs):
        raise OSError("déplacement impossible")

    monkeypatch.setattr("upscale.worker.shutil.move", fail)
    with pytest.raises(OSError):
        worker.restore("dddddddddddd")
    assert produced.exists() and trash.exists()


@needs_ffmpeg
def test_end_to_end_mp4_with_mov_text(env, fake_docker):
    st.save_settings({"model": "realesr-animevideov3"})
    make_video(env / "videos" / "serie" / "ep1.mp4", subtitles=True)
    [row] = process_all(env)
    assert row["status"] == "completed", job_log(row)
    assert row["warning"] is None, row["warning"]
    output = env / "out" / "serie" / "ep1.mkv"
    streams = ffprobe_streams(output)
    video = video_stream(output)
    assert (video["codec_name"], video["width"], video["height"]) == ("hevc", 960, 720)  # HEVC par défaut
    assert any(s["codec_type"] == "audio" for s in streams)
    assert any(s["codec_type"] == "subtitle" and s["codec_name"] == "subrip" for s in streams)
    assert sorted(p.name for p in (env / "out" / "serie").iterdir()) == ["ep1.mkv"]  # travail nettoyé
    run = next(c for c in calls(fake_docker) if c[:2] == ["docker", "run"])
    assert run[run.index("-s") + 1] == "3"  # 240p -> 720p : ×3 suffit
    assert run[run.index("-v") + 1].startswith("/srv/nas/media:")


@needs_ffmpeg
def test_new_destination_reprocesses(env, fake_docker):
    make_video(env / "videos" / "a.mp4")
    [first] = process_all(env)
    assert first["status"] == "completed"
    assert scanner.scan_files(st.get_settings())["files"] == []  # même sortie : déjà traité
    st.save_settings({"output_suffix": " - v2"})
    [second] = process_all(env)
    assert second["id"] == first["id"] and second["status"] == "completed"
    assert sorted(p.name for p in (env / "out").iterdir()) == ["a - v2.mkv", "a.mkv"]


@needs_ffmpeg
def test_cover_art_is_not_processed(env, fake_docker):
    plain = make_video(env.parent / "plain.mkv")
    cover = env.parent / "cover.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:size=600x600", "-frames:v", "1",
                    str(cover)], check=True)
    target = env / "videos" / "avec-couverture.mp4"
    # La couverture est le PREMIER flux vidéo du fichier.
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(cover), "-i", str(plain), "-map", "0:v", "-map", "1:v",
                    "-map", "1:a", "-c", "copy", "-disposition:v:0", "attached_pic", str(target)], check=True)
    info = media.probe_source(target)
    assert (info["width"], info["height"]) == (320, 240)
    [row] = process_all(env)
    assert row["status"] == "completed", job_log(row)
    assert video_stream(env / "out" / "avec-couverture.mkv")["height"] == 720


@needs_ffmpeg
def test_upscaling_starts_during_cut(env, fake_docker, monkeypatch):
    # Découpe volontairement lente (débruitage fort) : l'IA doit commencer avant qu'elle soit finie.
    monkeypatch.setattr(config, "SEGMENT_SECONDS", 1)
    st.save_settings({"denoise": "strong"})
    make_video(env / "videos" / "long.mp4", width=320, height=240, seconds=8, rate=25)
    [row] = process_all(env)
    assert row["status"] == "completed" and row["warning"] is None, job_log(row)
    log = job_log(row).splitlines()
    first_ai = next(i for i, line in enumerate(log) if line.startswith("$ docker run"))
    cut_done = next(i for i, line in enumerate(log) if "Découpe terminée" in line)
    assert first_ai < cut_done, "l'IA aurait dû démarrer pendant la découpe"
    assert row["segments_total"] == 8
    frames = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                             "stream=nb_read_frames", "-of", "csv=p=0", str(env / "out" / "long.mkv")],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert frames == "200"  # aucune image perdue entre les segments


@needs_ffmpeg
def test_segments_resume_after_pause(env, fake_docker, monkeypatch):
    monkeypatch.setattr(config, "SEGMENT_SECONDS", 1)
    make_video(env / "videos" / "long.mp4", seconds=3)
    settings = st.get_settings()
    scanner.enqueue(scanner.scan_files(settings)["files"], settings)
    reasons = iter([None, "En pause"])
    monkeypatch.setattr(pipeline, "defer_reason", lambda: next(reasons, None))
    job = worker.claim_next_job()
    pipeline.run_job(job)
    runner.set_active(None)
    row = db.get_job(job["id"])
    assert (row["status"], row["stage"], row["segments_done"], row["segments_total"]) == ("queued", "En pause", 1, 3)
    assert row["progress"] > 0
    monkeypatch.setattr(pipeline, "defer_reason", lambda: None)
    job = worker.claim_next_job()
    pipeline.run_job(job)
    row = db.get_job(job["id"])
    assert row["status"] == "completed", job_log(row)
    upscales = [c for c in calls(fake_docker) if c[:2] == ["docker", "run"] and "realesrgan" in c]
    assert len(upscales) == 3  # le segment déjà encodé n'a pas été refait
    info = media.probe_source(env / "out" / "long.mkv")
    assert abs(info["duration"] - 3.0) < 0.3


@needs_ffmpeg
def test_end_to_end_anamorphic_interlaced_hevc(env, fake_docker):
    st.save_settings({"codec": "hevc", "denoise": "light"})
    make_video(env / "videos" / "dvd.mkv", width=360, height=240,
               extra=["-vf", "setfield=tff", "-x264-params", "interlaced=1:tff=1", "-aspect", "16:9"])
    [row] = process_all(env)
    assert row["status"] == "completed", job_log(row)
    assert "bwdif" in job_log(row) and "hqdn3d" in job_log(row)
    video = video_stream(env / "out" / "dvd.mkv")
    assert (video["codec_name"], video["width"], video["height"], video["pix_fmt"]) == ("hevc", 1280, 720, "yuv420p10le")


@needs_ffmpeg
def test_end_to_end_interpolation_local_runner(env, fake_docker, monkeypatch):
    monkeypatch.setattr(config, "RUNNER", "local")
    st.save_settings({"interpolate": 2, "output_suffix": " - IA"})
    make_video(env / "videos" / "film.mp4")
    [row] = process_all(env)
    assert row["status"] == "completed", job_log(row)
    assert row["warning"] is None, row["warning"]
    video = video_stream(env / "out" / "film - IA.mkv")
    assert video["avg_frame_rate"] == "20/1"
    tools = [c for c in calls(fake_docker) if c[0] == "video2x"]
    assert any("rife" in c for c in tools) and not any(c[0] == "docker" and c[1] == "run" for c in calls(fake_docker))


@needs_ffmpeg
def test_disk_check_and_chown(env, fake_docker, monkeypatch):
    make_video(env / "videos" / "a.mp4")

    class Usage:
        free = 1024**2

    real_usage = pipeline.shutil.disk_usage
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda path: Usage)
    [row] = process_all(env)
    assert row["status"] == "failed" and "Espace disque insuffisant" in row["error"]

    monkeypatch.setattr(pipeline.shutil, "disk_usage", real_usage)
    monkeypatch.setattr(config, "PUID", 1234)
    monkeypatch.setattr(config, "PGID", 4321)
    worker.retry(row["id"])
    job = worker.claim_next_job()
    pipeline.run_job(job)
    output = env / "out" / "a.mkv"
    assert db.get_job(job["id"])["status"] == "completed"
    if os.geteuid() == 0:
        assert (output.stat().st_uid, output.stat().st_gid) == (1234, 4321)


@needs_ffmpeg
@root_only
def test_output_gets_owner_of_source_by_default(env, fake_docker):
    source = make_video(env / "videos" / "serie" / "a.mp4")
    os.chown(source, 1500, 1600)
    [row] = process_all(env)
    assert row["status"] == "completed"
    output = env / "out" / "serie" / "a.mkv"
    assert (output.stat().st_uid, output.stat().st_gid) == (1500, 1600)
    assert (env / "out" / "serie").stat().st_uid == 1500  # dossier créé par Upscale

    os.chown(output, 0, 0)  # comme avec une version précédente
    assert worker.fix_ownership() == 1
    assert output.stat().st_uid == 1500


def test_default_owner_never_picks_root(env, monkeypatch):
    probe = env / "videos" / "x.mkv"
    probe.write_bytes(b"x")
    if os.geteuid() == 0:
        assert runner.default_owner(probe, env / "videos") is None  # tout appartient à root ici
    monkeypatch.setattr(config, "PUID", 1000)
    assert runner.default_owner(probe, env / "videos") == (1000, -1)


@needs_ffmpeg
def test_multiple_disks_end_to_end(env, fake_docker, monkeypatch):
    make_video(env / "videos" / "film.mp4")
    [first] = process_all(env)
    assert (env / "out" / "film.mkv").exists()  # une seule source : comme avant

    disk = add_disk("Disque2")
    make_video(disk / "Series" / "S01" / "ep1.mp4")
    st.save_settings({"sources": [str(env / "videos"), str(disk / "Series")]})
    # Le film déjà traité n'est pas refait parce que l'organisation de la destination change.
    files = scanner.scan_files(st.get_settings())["files"]
    assert [Path(f["destination"]) for f in files] == [env / "out" / "Series" / "S01" / "ep1.mkv"]

    host = str(config.DISKS_ROOT.parent / "hote-disque2")
    monkeypatch.setenv("FAKE_DOCKER_MOUNTS", json.dumps([
        {"Type": "bind", "Source": "/srv/nas/media", "Destination": str(config.DATA_ROOT)},
        {"Type": "bind", "Source": host, "Destination": str(disk)},
        {"Type": "volume", "Source": "x", "Destination": "/app/state"},
        {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"},
    ]))
    monkeypatch.setattr(runner, "_mounts_cache", None)  # les montages ne changent qu'au redémarrage
    [row] = process_all(env)
    assert row["status"] == "completed", job_log(row)
    assert (env / "out" / "Series" / "S01" / "ep1.mkv").exists()
    run = [c for c in calls(fake_docker) if c[:2] == ["docker", "run"]][-1]
    mounts = [run[i + 1] for i, arg in enumerate(run) if arg == "-v"]
    assert mounts == [f"/srv/nas/media:{config.DATA_ROOT}", f"{host}:{disk}"]


def test_render_presets_and_filter_graph(env):
    assert st.render_params({"render": "ai"}) == (100, "off")
    assert st.render_params({"render": "peau-normale"}) == (70, "light")
    assert st.render_params({"render": "custom", "ai_strength": 55, "grain": "medium"}) == (55, "medium")
    graph, label = media.render_filter((1280, 720), 70, "light")
    assert label == "[out]"
    assert "blend=all_expr='A*0.70+B*0.30'" in graph[1] and "noise=c0s=4" in graph[1]
    pure, label = media.render_filter((1280, 720), 100, "off", ",format=nv12,hwupload")
    assert "blend" not in pure[1] and pure[1].endswith("format=nv12,hwupload[out]")
    command = media.segment_encode_command(Path("ai.mkv"), Path("o.mkv"), (1280, 720),
                                           {"render": "peau-normale", "codec": "hevc"}, original=Path("src.mkv"))
    assert command.count("-i") == 2 and "src.mkv" in command
    for bad in ({"render": "fort"}, {"ai_strength": 10}, {"grain": "lourd"}):
        with pytest.raises(ValueError):
            st.save_settings(bad)
    st.save_settings({"profiles": [{"path": str(env / "videos"), "render": "peau-normale"}]})
    assert st.effective_settings(st.get_settings(), env / "videos" / "x.mp4")["render"] == "peau-normale"


@needs_ffmpeg
def test_peau_normale_end_to_end(env, fake_docker, monkeypatch):
    monkeypatch.setattr(config, "SEGMENT_SECONDS", 1)
    st.save_settings({"render": "peau-normale"})
    make_video(env / "videos" / "scene.mp4", seconds=2)
    [row] = process_all(env)
    assert row["status"] == "completed" and row["warning"] is None, job_log(row)
    log = job_log(row)
    assert "Peau normale (70 % IA, grain light)" in log and "blend=all_expr" in log and "noise=c0s=4" in log
    out = env / "out" / "scene.mkv"
    frames = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                             "stream=nb_read_frames", "-of", "csv=p=0", str(out)],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert frames == "20" and video_stream(out)["height"] == 720
    jobs = app.test_client().get("/api/jobs").get_json()["jobs"]
    assert jobs[0]["render"] == "peau-normale"


def test_work_dir_must_be_an_accessible_directory(env):
    outside = "/tmp/upscale-work-hors-racine"
    with pytest.raises(ValueError, match="hors des dossiers accessibles"):
        st.save_settings({"work_dir": outside})
    file_path = env / "pas-un-dossier"
    file_path.write_text("x")
    with pytest.raises(ValueError, match="doit être un dossier"):
        st.save_settings({"work_dir": str(file_path)})


@needs_ffmpeg
def test_work_dir_keeps_temporary_files_out_of_destination(env, fake_docker):
    work = env / "work"
    saved = st.save_settings({"work_dir": str(work), "destination": str(env / "out"),
                              "sources": [str(env / "videos")]})
    assert saved["work_dir"] == str(work.resolve())
    make_video(env / "videos" / "film.mp4")
    [row] = process_all(env)
    assert row["status"] == "completed", job_log(row)
    assert (env / "out" / "film.mkv").exists()
    assert not list((env / "out").glob(".upscale-*"))
    assert not list(work.glob(".upscale-*"))


@needs_ffmpeg
def test_replace_original_and_restore(env, fake_docker):
    st.save_settings({"replace_original": True, "trash_days": 30})
    original = make_video(env / "videos" / "serie" / "film.avi", extra=["-f", "avi"])
    size = original.stat().st_size
    [row] = process_all(env)
    assert row["status"] == "completed", job_log(row)
    produced = env / "videos" / "serie" / "film.mkv"
    trash = env / "videos" / ".upscale-corbeille" / "serie" / "film.avi"
    assert not original.exists() and produced.exists() and trash.stat().st_size == size
    assert video_stream(produced)["codec_name"] == "hevc"
    assert row["destination"] == str(produced) and row["trash_path"] == str(trash)
    assert not (env / "out").exists()  # la destination n'est pas utilisée
    assert scanner.scan_files(st.get_settings())["files"] == []  # pas de retraitement en boucle

    assert app.test_client().post(f"/api/jobs/{row['id']}/restore", json={}).status_code == 200
    assert original.exists() and original.stat().st_size == size and not produced.exists() and not trash.exists()
    assert scanner.scan_files(st.get_settings())["files"] == []  # restauré : pas retraité non plus
    assert app.test_client().post(f"/api/jobs/{row['id']}/restore", json={}).status_code == 400


def test_replace_completed_output_from_destination(env):
    original = env / "videos" / "film.avi"
    original.write_bytes(b"original")
    destination = env / "out" / "film - 720p.mkv"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"nouvelle version")
    insert_job("dddddddddd01", status="completed")
    db.set_job("dddddddddd01", source=str(original), destination=str(destination), stage="Terminé")
    target = worker.replace_from_destination("dddddddddd01")
    trash = env / "videos" / ".upscale-corbeille" / "film.avi"
    assert target == original.parent / destination.name
    assert not original.exists() and target.read_bytes() == b"nouvelle version"
    assert trash.read_bytes() == b"original"
    row = db.get_job("dddddddddd01")
    assert row["destination"] == str(target) and row["trash_path"] == str(trash)
    assert app.test_client().post("/api/jobs/dddddddddd01/replace-original", json={}).status_code == 409

    worker.restore("dddddddddd01")
    assert original.read_bytes() == b"original" and not target.exists() and not trash.exists()


def test_replace_originals_bulk_and_immediate_delete(env):
    st.save_settings({"trash_days": 0})
    for index in (1, 2):
        original = env / "videos" / f"film{index}.mkv"
        original.write_bytes(b"original")
        destination = env / "out" / f"film{index}.mkv"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"nouvelle")
        job_id = f"eeeeeeeeee0{index}"
        insert_job(job_id, status="completed")
        db.set_job(job_id, source=str(original), destination=str(destination), stage="Terminé")
    result = app.test_client().post("/api/jobs/replace-originals", json={})
    assert result.status_code == 200
    assert result.get_json()["replaced"] == 2
    for index in (1, 2):
        assert (env / "videos" / f"film{index}.mkv").read_bytes() == b"nouvelle"
        assert not (env / "out" / f"film{index}.mkv").exists()
    assert not any((env / "videos" / ".upscale-corbeille").rglob("*"))


@needs_ffmpeg
def test_replace_in_place_and_immediate_delete(env, fake_docker):
    st.save_settings({"replace_original": True, "trash_days": 0})
    original = make_video(env / "videos" / "clip.mkv")
    [row] = process_all(env)
    assert row["status"] == "completed", job_log(row)
    assert video_stream(original)["codec_name"] == "hevc"  # même nom, nouvelle version
    assert row["trash_path"] is None and not any((env / "videos" / ".upscale-corbeille").rglob("*.mkv"))


@needs_ffmpeg
def test_replace_keeps_original_when_verification_fails(env, fake_docker, monkeypatch):
    st.save_settings({"replace_original": True})
    original = make_video(env / "videos" / "film.mp4")
    monkeypatch.setattr(media, "verify_output", lambda *a, **k: ["durée 0.5 s au lieu de 1.0 s"])
    [row] = process_all(env)
    assert row["status"] == "completed" and "original conservé" in row["warning"]
    assert original.exists() and (env / "videos" / "film - Upscale.mkv").exists()
    assert row["trash_path"] is None


def test_purge_trash(env):
    trash = env / "videos" / ".upscale-corbeille" / "a" / "vieux.avi"
    trash.parent.mkdir(parents=True)
    trash.write_bytes(b"x")
    st.save_settings({"trash_days": 30})
    assert worker.purge_trash(time.time() + 10 * 86400) == 0 and trash.exists()
    assert worker.purge_trash(time.time() + 31 * 86400) == 1
    assert not trash.exists() and not trash.parent.exists()


def test_verify_output_reports_problems(env):
    if not os.path.exists("/usr/bin/ffprobe") and not subprocess.run(["which", "ffprobe"]).returncode == 0:
        pytest.skip("ffprobe requis")
    clip = make_video(env / "videos" / "v.mkv")
    problems = media.verify_output(clip, {"duration": 30.0, "audio": ["aac", "ac3"], "fps": 10}, 1)
    assert any("durée" in p for p in problems) and any("audio" in p for p in problems)
    assert any("sous-titres" in p for p in problems)


@needs_ffmpeg
@needs_ffmpeg
def test_job_thumbnail_is_extracted_from_the_video(env):
    source = env / "videos" / "film.mkv"
    make_video(source, width=320, height=240)
    job_id = "cccccccccccc"
    with db.connect() as conn:
        conn.execute("""INSERT INTO jobs(id,source,destination,source_size,source_mtime_ns,status,stage,
                        created_at,updated_at,kind) VALUES(?,?,?,1,1,'queued','En attente',?,?,'upscale')""",
                     (job_id, str(source), str(env / "out" / "film.mkv"), db.now(), db.now()))
    client = app.test_client()
    response = client.get(f"/api/jobs/{job_id}/thumbnail")
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg" and len(response.data) > 1000
    thumb = config.thumbnail_dir() / f"{job_id}.jpg"
    assert thumb.exists()
    mtime = thumb.stat().st_mtime_ns
    assert client.get(f"/api/jobs/{job_id}/thumbnail").status_code == 200
    assert thumb.stat().st_mtime_ns == mtime
    assert client.get("/api/jobs/dddddddddddd/thumbnail").status_code == 404
    db.set_job(job_id, source=str(env / "videos" / "absent.mkv"))
    assert client.get(f"/api/jobs/{job_id}/thumbnail").status_code == 404


def test_preview_delete_removes_files(env):
    job_id = "eeeeeeeeeeee"
    folder = config.preview_dir() / job_id
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text("{}", encoding="utf-8")
    (folder / "apercu.mp4").write_bytes(b"x")
    with db.connect() as conn:
        conn.execute("""INSERT INTO jobs (id,source,destination,source_size,source_mtime_ns,status,stage,
                                          created_at,updated_at,kind)
                        VALUES(?,?,?,0,0,'completed','Prêt','2026-01-01','2026-01-01','preview')""",
                     (job_id, str(env / "videos" / "film.mkv"), str(folder)))
    response = app.test_client().post(f"/api/jobs/{job_id}/delete", json={})
    assert response.status_code == 200
    assert not folder.exists()
    assert db.get_job(job_id) is None


def test_preview_generates_variants(env, fake_docker):
    source = make_video(env / "videos" / "film.mp4", seconds=3)
    client = app.test_client()
    assert client.post("/api/preview", json={"source": str(source), "duration": 1,
                                             "models": ["realesrgan-plus"]}).status_code == 400
    response = client.post("/api/preview", json={"source": str(source), "start": 0.5, "duration": 2,
                                                  "models": ["realesrgan-plus", "realesr-animevideov3"]})
    assert response.status_code == 202
    job = worker.claim_next_job()
    pipeline.run_job(job)
    row = db.get_job(job["id"])
    assert row["status"] == "completed", job_log(row)
    previews = client.get("/api/previews").get_json()["previews"]
    manifest = previews[0]["manifest"]
    assert manifest["complete"] and [v["id"] for v in manifest["variants"]] == [
        "original", "config", "realesrgan-plus", "realesrgan-plus-peau-normale",
        "realesr-animevideov3", "realesr-animevideov3-peau-normale"]
    assert manifest["variants"][1]["label"].startswith("Votre configuration · Real-ESRGAN Plus")
    assert video_stream(config.preview_dir() / job["id"] / "realesrgan-plus-peau-normale.mp4")["height"] == 720
    assert video_stream(config.preview_dir() / job["id"] / "realesrgan-plus.mp4")["height"] == 720
    ranged = client.get(f"/api/previews/{job['id']}/original.mp4", headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206 and len(ranged.data) == 100
    assert not any(p.name.startswith(".upscale-preview") for p in (env / "out").iterdir())


def test_recover_removes_orphan_containers(env, fake_docker):
    insert_job("cccccccccccc", status="running")
    worker.recover_interrupted_jobs()
    assert any(c[1:3] == ["ps", "-aq"] for c in calls(fake_docker))
    assert db.get_job("cccccccccccc")["status"] == "queued"


def test_throughput_estimates_from_measured_speed():
    """Temps restant d'après la vitesse mesurée : ni la découpe ni les segments repris ne comptent."""
    clock = [1000.0]
    speed = runner.Throughput(1000, clock=lambda: clock[0])
    speed.skip(200)                      # segments déjà faits avant un redémarrage
    assert speed.estimate() == (None, None)
    clock[0] += 500                      # découpe, pause : hors mesure
    speed.begin()
    clock[0] += 10
    speed.advance(20)
    assert speed.estimate() == (None, None)   # moins de 20 s : pas d'annonce
    clock[0] += 40
    speed.advance(100)
    assert speed.estimate() == (2.0, 350)     # 100 images en 50 s ; reste 700 images
    speed.end(100)
    clock[0] += 300                      # attente entre deux segments : hors mesure
    assert speed.estimate() == (2.0, 350)
    speed.begin()
    clock[0] += 50
    speed.advance(100)
    assert speed.estimate() == (2.0, 300)


@needs_ffmpeg
def test_sd_source_upscaled_to_hd_keeps_its_colours(tmp_path):
    """Source 360p (BT.601) -> 720p : la sortie est convertie et étiquetée BT.709, les couleurs sont justes."""
    source = tmp_path / "sd.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "smptebars=size=640x360:rate=5:duration=1",
                    "-vf", "scale=out_color_matrix=smpte170m,format=yuv420p", "-c:v", "ffv1", str(source)], check=True)
    info = {"height": 360, "color_space": ""}
    plan = media.color_plan(info, 720)
    assert plan == {"src": "smpte170m", "dst": "bt709"}
    assert media.color_plan(info, 480) == {"src": "smpte170m", "dst": "smpte170m"}   # sortie SD : inchangée
    assert media.color_plan({"height": 1080, "color_space": "bt709"}, 2160) == {"src": "bt709", "dst": "bt709"}
    assert media.color_plan({"height": 2160, "color_space": "bt2020nc"}, 2160) is None  # HDR : non touché
    settings = {"codec": "h264", "crf": 12, "preset": "ultrafast", "render": "ai"}
    fixed, legacy = tmp_path / "fixed.mkv", tmp_path / "legacy.mkv"
    subprocess.run(media.segment_encode_command(source, fixed, (1280, 720), settings, color=plan),
                   check=True, capture_output=True)
    subprocess.run(media.segment_encode_command(source, legacy, (1280, 720), settings), check=True, capture_output=True)
    tagged = media.ffprobe_json(fixed, "-select_streams", "v:0", "-show_entries", "stream=color_space")
    assert tagged["streams"][0]["color_space"] == "bt709"
    for x in (5.5 / 7, 3.5 / 7, 1.5 / 7):   # barres rouge, verte, jaune
        expected = _patch_rgb(source, "smpte170m", x, 0.3)
        good = _patch_rgb(fixed, "bt709", x, 0.3)          # un lecteur lit du 720p en BT.709
        assert max(abs(a - b) for a, b in zip(good, expected, strict=True)) <= 6, (expected, good)
    red_expected = _patch_rgb(source, "smpte170m", 5.5 / 7, 0.3)
    red_before = _patch_rgb(legacy, "bt709", 5.5 / 7, 0.3)   # ancien comportement : teintes décalées
    assert max(abs(a - b) for a, b in zip(red_before, red_expected, strict=True)) > 10, (red_expected, red_before)
