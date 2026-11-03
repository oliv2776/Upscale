from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from conftest import calls, make_video, needs_ffmpeg

from upscale import config, db, gpu, media, notify, ollama, pipeline, runner, scanner, worker
from upscale import settings as st
from upscale.web import app

# ================================================================== réglages


def test_safe_path_rejects_outside_and_relative(env):
    with pytest.raises(ValueError):
        st.safe_path("/etc")
    with pytest.raises(ValueError):
        st.safe_path("relative/path")
    assert st.safe_path(str(env / "videos"), must_exist=True) == (env / "videos").resolve()


def test_validate_folders_rejects_nesting(env):
    base = st.get_settings()
    videos = str(env / "videos")
    with pytest.raises(ValueError, match="dans un dossier source"):
        st.validate_folders({**base, "destination": str(env / "videos" / "out")})
    (env / "out" / "videos").mkdir(parents=True)
    with pytest.raises(ValueError, match="source ne peut pas"):
        st.validate_folders({**base, "sources": [str(env / "out" / "videos")], "destination": str(env / "out")})
    with pytest.raises(ValueError):
        st.validate_folders({**base, "destination": str(env)})
    (env / "videos" / "Films").mkdir()
    with pytest.raises(ValueError, match="déjà inclus"):
        st.validate_folders({**base, "sources": [videos, str(env / "videos" / "Films")]})
    with pytest.raises(ValueError, match="au moins un"):
        st.validate_folders({**base, "sources": [], "source": ""})


def test_partial_save_does_not_need_valid_folders(env):
    db.store_settings_payload({**st.get_settings(), "source": "/nexistepas"})
    saved = st.save_settings({"ollama_enabled": True, "ollama_url": "http://10.0.0.2:11434/"})
    assert saved["ollama_enabled"] is True and saved["ollama_url"] == "http://10.0.0.2:11434"
    with pytest.raises(ValueError):
        st.save_settings({"source": "/nexistepas"})


def test_secrets_are_masked_and_kept(env):
    st.save_settings({"notify_type": "ntfy", "notify_url": "https://ntfy.sh/x", "notify_token": "s3cret"})
    public = app.test_client().get("/api/config").get_json()["settings"]
    assert public["notify_token"] == "" and public["notify_token_set"] is True
    st.save_settings({"notify_token": ""})
    assert st.get_settings()["notify_token"] == "s3cret"
    st.save_settings({"notify_token_clear": True})
    assert st.get_settings()["notify_token"] == ""


def test_retired_model_is_migrated(env):
    stored = st.get_settings()
    db.store_settings_payload({**stored, "model": "realesr-generalv3",
                               "profiles": [{"path": str(env / "videos"), "model": "realesr-generalv3"}]})
    settings = st.get_settings()
    assert settings["model"] == "realesrgan-plus" and settings["profiles"][0]["model"] == "realesrgan-plus"
    assert st.save_settings({"model": "realesr-generalv3"})["model"] == "realesrgan-plus"


def test_validation_errors(env):
    for payload in ({"codec": "mpeg2"}, {"denoise": "extreme"}, {"schedule_start": "25:00"},
                    {"output_suffix": "a/b"}, {"interpolate": 4}, {"model": "nope"}, {"notify_url": "ftp://x"}):
        with pytest.raises(ValueError):
            st.save_settings(payload)


def test_profiles_longest_match(env):
    anime = env / "videos" / "Animation"
    kids = anime / "Enfants"
    st.save_settings({"profiles": [
        {"path": str(anime), "model": "realesr-animevideov3"},
        {"path": str(kids), "model": "realesrgan-plus-anime", "output_height": 1080, "denoise": "light"},
    ]})
    settings = st.get_settings()
    effective = st.effective_settings(settings, kids / "a.mkv")
    assert effective["model"] == "realesrgan-plus-anime" and effective["output_height"] == 1080
    assert effective["profile"] == str(kids)
    assert st.effective_settings(settings, anime / "b.mkv")["model"] == "realesr-animevideov3"
    assert st.effective_settings(settings, env / "videos" / "c.mkv")["model"] == "realesrgan-plus"
    assert "notify_token" not in effective and "profiles" not in effective
    with pytest.raises(ValueError):
        st.save_settings({"profiles": [{"path": "/etc", "model": "realesrgan-plus"}]})


# ================================================================== calculs et commandes


@pytest.mark.parametrize(("model", "source", "output", "expected"), [
    ("realesr-animevideov3", 480, 720, 2),
    ("realesr-animevideov3", 240, 720, 3),
    ("realesr-animevideov3", 240, 1080, 4),
    ("realesr-animevideov3", 144, 1080, 4),
    ("realesrgan-plus", 480, 720, 4),
])
def test_choose_scale(model, source, output, expected):
    assert media.choose_scale(model, source, output) == expected


def test_output_size_and_names():
    assert media.output_size({"width": 720, "height": 480, "sar": 32 / 27}, 720) == (1280, 720)
    assert media.output_size({"width": 640, "height": 480, "sar": 1.0}, 720) == (960, 720)
    assert media.output_relative(Path("a/film.avi"), False) == Path("a/film.mkv")
    assert media.output_relative(Path("a/film.avi"), True) == Path("a/film.avi.mkv")
    assert media.output_relative(Path("Film (2001).avi"), False, " - 720p IA") == Path("Film (2001) - 720p IA.mkv")


def test_encoder_args():
    assert media.encoder_args({"codec": "h264", "crf": 16, "preset": "slow"})[2][:2] == ["-c:v", "libx264"]
    hevc = media.encoder_args({"codec": "hevc", "crf": 16, "preset": "slow"})[2]
    assert hevc[hevc.index("-crf") + 1] == "18" and "yuv420p10le" in hevc
    av1 = media.encoder_args({"codec": "av1", "crf": 16, "preset": "slow"})[2]
    assert av1[av1.index("-preset") + 1] == "4" and av1[av1.index("-crf") + 1] == "26"
    pre, vf, args = media.encoder_args({"codec": "hevc_vaapi", "crf": 18, "preset": "slow"})
    assert pre == ["-vaapi_device", config.VAAPI_DEVICE] and vf.endswith("hwupload") and "hevc_vaapi" in args


def test_prefilters():
    assert media.prefilters({"deinterlace": "auto", "denoise": "off"}, {"field_order": "tt"}) == ["bwdif=mode=send_frame"]
    assert media.prefilters({"deinterlace": "off", "denoise": "strong"}, {"field_order": "tt"}) == ["nlmeans=s=3:p=7:r=15"]
    assert media.prefilters({"deinterlace": "auto", "denoise": "off"}, {"field_order": "progressive"}) == []


def test_subtitle_and_audio_args():
    args, skipped = media.subtitle_args(["subrip", "mov_text", "eia_608", "hdmv_pgs_subtitle"])
    assert args == ["-map", "1:s:0", "-c:s:0", "copy", "-map", "1:s:1", "-c:s:1", "srt",
                    "-map", "1:s:3", "-c:s:2", "copy"]
    assert skipped == ["#2 (eia_608)"]
    assert media.audio_args(["ac3", "pcm_dvd"]) == ["-map", "1:a?", "-c:a", "copy", "-c:a:1", "flac"]


def test_video2x_command_modes(env, monkeypatch):
    monkeypatch.setattr(config, "VIDEO2X_GPU", "1")
    command, container = runner.video2x_command("abc123def456", ["-i", "x"])
    assert command[command.index("-v") + 1] == f"/srv/nas/media:{env.resolve()}"
    assert container == "upscale-abc123def456" and config.CONTAINER_LABEL in command
    assert command[-2:] == ["-d", "1"]
    monkeypatch.setattr(config, "RUNNER", "local")
    command, container = runner.video2x_command("abc123def456", ["-i", "x"])
    assert command == [config.VIDEO2X_BIN, "-i", "x", "-d", "1"] and container is None


def test_progress_parsers():
    assert runner.parse_video2x_progress("\rProcessing frame 250/1000 (25.00%); elapsed") == pytest.approx(0.25)
    assert runner.parse_video2x_progress("[info] frame 3 / 4") == pytest.approx(0.75)
    assert runner.parse_video2x_progress("Loading model 100%") is None
    parse = runner.ffmpeg_progress_parser(10.0)
    assert parse("out_time_us=5000000") == pytest.approx(0.5)
    assert parse("fps=24") is None


def test_within_schedule():
    cfg = {"schedule_enabled": True, "schedule_start": "22:00", "schedule_end": "07:00"}
    assert worker.within_schedule(cfg, datetime(2026, 1, 1, 23, 30))
    assert worker.within_schedule(cfg, datetime(2026, 1, 1, 6, 59))
    assert not worker.within_schedule(cfg, datetime(2026, 1, 1, 12, 0))
    day = {**cfg, "schedule_start": "09:00", "schedule_end": "17:00"}
    assert worker.within_schedule(day, datetime(2026, 1, 1, 12, 0))
    assert not worker.within_schedule(day, datetime(2026, 1, 1, 18, 0))
    assert worker.within_schedule({**cfg, "schedule_enabled": False}, datetime(2026, 1, 1, 12, 0))


# ================================================================== analyse et file


@needs_ffmpeg
def test_scan_disambiguates_collisions_and_uses_cache(env, monkeypatch):
    videos = env / "videos"
    make_video(videos / "film.mp4")
    make_video(videos / "film.avi", extra=["-f", "avi"])
    make_video(videos / "autre.mkv")
    make_video(videos / "grand.mp4", width=1280, height=720)
    result = scanner.scan_files(st.get_settings())
    assert sorted(Path(f["destination"]).name for f in result["files"]) == ["autre.mkv", "film.avi.mkv", "film.mp4.mkv"]
    probed = []
    monkeypatch.setattr(media, "probe_dimensions", lambda path: probed.append(path))
    assert len(scanner.scan_files(st.get_settings())["files"]) == 3 and probed == []


@needs_ffmpeg
def test_scan_skips_files_being_copied(env):
    path = make_video(env / "videos" / "copie.mp4")
    assert scanner.scan_files(st.get_settings(), min_age_seconds=120)["too_recent"] == 1
    old = time.time() - 600
    os.utime(path, (old, old))
    assert len(scanner.scan_files(st.get_settings(), min_age_seconds=120)["files"]) == 1


@needs_ffmpeg
def test_enqueue_freezes_settings(env):
    make_video(env / "videos" / "anime" / "a.mp4")
    st.save_settings({"profiles": [{"path": str(env / "videos" / "anime"), "model": "realesr-animevideov3"}]})
    settings = st.get_settings()
    files = scanner.scan_files(settings)["files"]
    assert scanner.enqueue(files, settings)["added"] == 1
    st.save_settings({"output_height": 1080, "profiles": []})
    job = worker.claim_next_job()
    frozen = pipeline.job_settings(job)
    assert frozen["model"] == "realesr-animevideov3" and frozen["output_height"] == 720
    again = scanner.scan_files(st.get_settings())
    assert again["files"] == [] and again["already_known"] == 1
    other = {**files[0], "source": str(env / "videos" / "b.mp4"), "mtime_ns": 1}
    assert scanner.enqueue([other], st.get_settings()) == {"added": 0, "conflicts": 1}


def insert_job(job_id: str, status: str = "queued", position: float = 1.0, **extra) -> None:
    with db.connect() as conn:
        conn.execute("""INSERT INTO jobs(id,source,destination,source_size,source_mtime_ns,status,stage,
                        created_at,updated_at,position,kind,priority) VALUES(?,?,?,1,?,?,'',?,?,?,?,?)""",
                     (job_id, f"/s/{job_id}", f"/d/{job_id}.mkv", position, status, db.now(), db.now(), position,
                      extra.get("kind", "upscale"), extra.get("priority", 0)))


def test_cancel_before_claim_wins(env):
    insert_job("aaaaaaaaaaaa")
    assert app.test_client().post("/api/jobs/aaaaaaaaaaaa/cancel", json={}).status_code == 200
    assert worker.claim_next_job() is None
    assert db.get_job("aaaaaaaaaaaa")["status"] == "cancelled"


def test_queue_management(env):
    for index, job_id in enumerate(["aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3"]):
        insert_job(job_id, position=float(index))
    client = app.test_client()
    assert client.post("/api/jobs/aaaaaaaaaaa3/move", json={"to": "top"}).status_code == 200
    assert client.post("/api/jobs/aaaaaaaaaaa1/move", json={"to": "down"}).status_code == 200
    order = [j["id"] for j in client.get("/api/jobs?status=queued").get_json()["jobs"]]
    assert order == ["aaaaaaaaaaa3", "aaaaaaaaaaa2", "aaaaaaaaaaa1"]
    assert worker.claim_next_job()["id"] == "aaaaaaaaaaa3"

    db.set_job("aaaaaaaaaaa3", status="failed")
    assert client.post("/api/jobs/aaaaaaaaaaa3/retry", json={}).status_code == 200
    assert db.get_job("aaaaaaaaaaa3")["status"] == "queued"
    assert client.post("/api/jobs/aaaaaaaaaaa2/retry", json={}).status_code == 400

    db.set_job("aaaaaaaaaaa1", status="completed")
    assert client.post("/api/jobs/clear", json={"statuses": ["completed"]}).get_json()["deleted"] == 1
    assert client.post("/api/jobs/aaaaaaaaaaa2/delete", json={}).status_code == 200
    data = client.get("/api/jobs?limit=1&page=1").get_json()
    assert data["total"] == 1 and len(data["jobs"]) == 1


def test_previews_jump_the_queue_and_ignore_pause(env):
    insert_job("aaaaaaaaaaa1", position=1.0)
    insert_job("bbbbbbbbbbb1", position=2.0, kind="preview", priority=100)
    db.set_paused(True)
    assert worker.claim_next_job()["id"] == "bbbbbbbbbbb1"
    runner.set_active(None)
    assert worker.claim_next_job() is None


def test_watch_tick(env, monkeypatch):
    started = []
    monkeypatch.setattr(scanner, "start_scan", lambda *a, **k: started.append(k) or True)
    assert worker.watch_tick(1000.0) is False
    st.save_settings({"watch_enabled": True, "watch_interval": 10})
    assert worker.watch_tick(1000.0) is True and started[0]["min_age_seconds"] == 120
    assert worker.watch_tick(1000.0 + 300) is False
    assert worker.watch_tick(1000.0 + 601) is True


def test_maintenance(env, monkeypatch):
    monkeypatch.setattr(config, "PREVIEW_KEEP", 1)
    (config.log_dir() / "ffffffffffff.log").write_text("orphelin")
    with db.connect() as conn:
        conn.execute("INSERT INTO probe_cache VALUES('/nexiste/pas.mkv',1,1,10,10,?)", (db.now(),))
    for index in range(3):
        job_id = f"ccccccccccc{index}"
        insert_job(job_id, status="completed", kind="preview")
        (config.preview_dir() / job_id).mkdir(parents=True)
        db.set_job(job_id, updated_at=f"2026-01-0{index + 1}T00:00:00+00:00")
    result = worker.maintenance()
    assert result == {"logs": 1, "probe_cache": 1, "previews": 2, "owners": 0, "trash": 0}
    assert sorted(p.name for p in config.preview_dir().iterdir()) == ["ccccccccccc2"]


def test_run_process_cancellation(env):
    job_id = "bbbbbbbbbbbb"
    runner.set_active(job_id)
    threading.Timer(0.5, runner.request_cancel, args=(job_id,)).start()
    started = time.monotonic()
    with pytest.raises(runner.JobCancelled):
        runner.run_process(job_id, ["sleep", "30"])
    assert time.monotonic() - started < 10


# ================================================================== sécurité HTTP


def test_post_requires_json_and_same_origin(env):
    client = app.test_client()
    assert client.post("/api/pause", data="{}", content_type="text/plain").status_code == 415
    assert client.post("/api/pause", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/pause", json={}, headers={"Origin": "http://localhost"}).status_code == 200
    proxied = {"Origin": "https://upscale.maison.lan", "X-Forwarded-Host": "upscale.maison.lan"}
    assert client.post("/api/pause", json={}, headers=proxied).status_code == 200


def test_basic_auth(env, monkeypatch):
    monkeypatch.setattr(config, "AUTH_PASSWORD", "secret")
    client = app.test_client()
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/ping").status_code == 200
    token = base64.b64encode(b"admin:secret").decode()
    assert client.get("/api/jobs", headers={"Authorization": f"Basic {token}"}).status_code == 200


def test_bad_ids_rejected(env):
    client = app.test_client()
    assert client.get("/api/logs/..%2f..%2fetc").status_code in {400, 404}
    assert client.get("/api/previews/aaaaaaaaaaaa/..%2fx.mp4").status_code == 404


# ================================================================== bout en bout (faux Video2X)


def ffprobe_streams(path: Path) -> list[dict]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,avg_frame_rate,pix_fmt",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["streams"]


def video_stream(path: Path) -> dict:
    return next(s for s in ffprobe_streams(path) if s["codec_type"] == "video")


def process_all(env) -> list:
    settings = st.get_settings()
    scanner.enqueue(scanner.scan_files(settings)["files"], settings)
    rows = []
    while (job := worker.claim_next_job()) is not None:
        pipeline.run_job(job)
        runner.set_active(None)
        rows.append(db.get_job(job["id"]))
    return rows


def job_log(row) -> str:
    return (config.log_dir() / f"{row['id']}.log").read_text()


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


root_only = pytest.mark.skipif(os.geteuid() != 0, reason="chown vers un autre utilisateur : root requis")


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


def test_legacy_names_still_work(env, monkeypatch):
    monkeypatch.setenv("USCALE_SEGMENT_SECONDS", "42")
    assert config._env("UPSCALE_SEGMENT_SECONDS", "180") == "42"
    monkeypatch.setenv("UPSCALE_SEGMENT_SECONDS", "60")
    assert config._env("UPSCALE_SEGMENT_SECONDS", "180") == "60"
    state = config.STATE_DIR
    config.db_path().rename(state / "uscale.sqlite3")
    db.init_db()
    assert config.db_path().exists() and not (state / "uscale.sqlite3").exists()
    assert st.get_settings()["preset"] == "ultrafast"  # réglages conservés
    # Dossier de travail d'une tâche interrompue sous l'ancien nom : repris tel quel.
    out = env / "out"
    insert_job("dddddddddddd")
    db.set_job("dddddddddddd", destination=str(out / "f.mkv"))
    (out / ".uscale-dddddddddddd" / "enc").mkdir(parents=True)
    work = pipeline.upscale_work_dir(db.get_job("dddddddddddd"))
    assert work.name == ".upscale-dddddddddddd" and (work / "enc").is_dir()


def add_disk(name: str) -> Path:
    disk = config.DISKS_ROOT / name
    disk.mkdir(parents=True)
    return disk.resolve()


def test_disks_are_browsable_roots(env):
    disk = add_disk("Disque2")
    (disk / "Series").mkdir()
    assert config.roots() == [config.DATA_ROOT, disk]
    assert st.safe_path(str(disk / "Series"), must_exist=True) == disk / "Series"
    with pytest.raises(ValueError, match="hors des dossiers accessibles"):
        st.safe_path(str(config.DISKS_ROOT.parent))
    client = app.test_client()
    home = client.get("/api/browse").get_json()
    assert [d["path"] for d in home["disks"]] == [str(config.DATA_ROOT), str(disk)]
    inside = client.get("/api/browse", query_string={"path": str(disk)}).get_json()
    assert inside["parent"] == "" and [d["name"] for d in inside["directories"]] == ["Series"]
    with pytest.raises(ValueError, match="sous-dossier d'un disque"):
        st.validate_folders({**st.get_settings(), "destination": str(disk)})


def test_legacy_single_source_settings(env):
    payload = {k: v for k, v in st.get_settings().items() if k != "sources"}
    payload["source"] = str(env / "videos")
    db.store_settings_payload(payload)
    assert st.get_settings()["sources"] == [str(env / "videos")]


def test_source_labels_are_unique():
    labels = scanner.source_labels([Path("/data/Films"), Path("/disques/B/Films"), Path("/disques/C/Series")])
    assert list(labels.values()) == ["Films", "Films-2", "Series"]


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


def test_hevc_default_and_migration(env):
    assert st.DEFAULT_SETTINGS["codec"] == "hevc"
    payload = {k: v for k, v in st.get_settings().items() if k != "settings_version"}
    db.store_settings_payload({**payload, "codec": "h264"})  # réglages d'une version précédente
    st.migrate()
    assert st.get_settings()["codec"] == "hevc"
    st.save_settings({"codec": "h264"})  # choix explicite après migration : respecté
    st.migrate()
    assert st.get_settings()["codec"] == "h264"


def test_scan_ignores_upscale_internal_folders(env):
    for folder in (".upscale-abc123def456/src", ".upscale-corbeille", ".uscale-old"):
        (env / "videos" / folder).mkdir(parents=True)
        (env / "videos" / folder / "seg.mkv").write_bytes(b"x")
    (env / "videos" / "vrai.mkv").write_bytes(b"x")
    names = [p.name for p in scanner.list_candidates(env / "videos", True)]
    assert names == ["vrai.mkv"]


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
        "original", "realesrgan-plus", "realesrgan-plus-peau-normale",
        "realesr-animevideov3", "realesr-animevideov3-peau-normale"]
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


def test_gpu_check_is_async_and_cached(env, fake_docker):
    client = app.test_client()
    assert client.post("/api/health/gpu", json={}).status_code == 202
    for _ in range(50):
        state = client.get("/api/health").get_json()["gpu"]
        if state["status"] != "running":
            break
        time.sleep(0.1)
    assert state["status"] == "ok" and "RX 5500 XT" in state["devices"][0]


def test_gpu_stats_from_sysfs(env, tmp_path, monkeypatch):
    drm = tmp_path / "drm"
    device = tmp_path / "pci" / "0000:03:00.0"
    (device / "hwmon" / "hwmon3").mkdir(parents=True)
    (tmp_path / "drivers" / "amdgpu").mkdir(parents=True)
    (device / "driver").symlink_to(tmp_path / "drivers" / "amdgpu")
    for name, value in {"gpu_busy_percent": "87", "mem_info_vram_used": str(2 * 1024**3),
                        "mem_info_vram_total": str(4 * 1024**3)}.items():
        (device / name).write_text(value)
    for name, value in {"temp1_input": "63000", "power1_average": "95000000", "fan1_input": "1500"}.items():
        (device / "hwmon" / "hwmon3" / name).write_text(value)
    (drm / "card1").mkdir(parents=True)
    (drm / "card1" / "device").symlink_to(device)
    (drm / "card1-HDMI-A-1").mkdir()
    monkeypatch.setattr(config, "SYSFS_DRM", drm)
    [card] = app.test_client().get("/api/gpu/stats").get_json()["cards"]
    assert card == {"card": "card1", "busy_percent": 87, "vram_used": 2 * 1024**3, "vram_total": 4 * 1024**3,
                    "temperature_c": 63.0, "power_w": 95.0, "fan_rpm": 1500}
    assert gpu.stats()[0]["busy_percent"] == 87


def test_scan_endpoint_runs_in_background(env):
    client = app.test_client()
    settings = st.get_settings()
    assert client.post("/api/scan", json={"source": settings["source"],
                                          "destination": settings["destination"]}).status_code == 202
    for _ in range(50):
        state = client.get("/api/scan").get_json()
        if not state["running"]:
            break
        time.sleep(0.1)
    assert state["found"] == 0 and state["error"] is None


# ================================================================== services externes (faux serveur HTTP)


def test_ntfy_gotify_webhook(env, http_server):
    base, recorder = http_server
    notify.send({"notify_type": "ntfy", "notify_url": f"{base}/upscale", "notify_token": "tk"},
                "failed", "Upscale : échec", "film.mkv")
    ntfy = recorder.received[-1]
    assert ntfy["path"].startswith("/upscale?") and "title=Upscale" in ntfy["path"] and "priority=high" in ntfy["path"]
    assert ntfy["headers"]["Authorization"] == "Bearer tk" and ntfy["body"] == b"film.mkv"
    notify.send({"notify_type": "gotify", "notify_url": base, "notify_token": "gk"}, "completed", "T", "M")
    gotify = recorder.received[-1]
    assert gotify["path"] == "/message" and gotify["headers"]["X-Gotify-Key"] == "gk"
    assert json.loads(gotify["body"])["title"] == "T"
    notify.send({"notify_type": "webhook", "notify_url": f"{base}/hook"}, "completed", "T", "M", {"job_id": "x"})
    assert json.loads(recorder.received[-1]["body"]) == {"event": "completed", "title": "T", "message": "M", "job_id": "x"}

    st.save_settings({"notify_type": "webhook", "notify_url": f"{base}/hook"})
    assert app.test_client().post("/api/notify/test", json={}).status_code == 200


def test_media_server_refresh(env, http_server):
    base, recorder = http_server
    assert "Jellyfin" in notify.refresh_media_server({"media_type": "jellyfin", "media_url": base, "media_token": "j"})
    assert recorder.received[-1]["path"] == "/Library/Refresh"
    assert recorder.received[-1]["headers"]["X-Emby-Token"] == "j"
    recorder.responses["/library/sections"] = (200, json.dumps(
        {"MediaContainer": {"Directory": [{"key": "1"}, {"key": "4"}]}}).encode())
    message = notify.refresh_media_server({"media_type": "plex", "media_url": base, "media_token": "p"})
    assert "2 bibliothèque" in message
    paths = [r["path"] for r in recorder.received[-2:]]
    assert paths == ["/library/sections/1/refresh?X-Plex-Token=p", "/library/sections/4/refresh?X-Plex-Token=p"]


@needs_ffmpeg
def test_ollama_auto_model(env, http_server):
    base, recorder = http_server
    recorder.responses["/api/generate"] = (200, json.dumps({"response": "ANIMATION"}).encode())
    clip = make_video(env / "videos" / "dessin.mp4", seconds=2)
    settings = {"ollama_enabled": True, "ollama_url": base, "ollama_model": "llava"}
    model, reason = ollama.classify(settings, clip, 2.0)
    assert model == st.ANIMATION_MODEL and "animation" in reason
    request = json.loads(recorder.received[-1]["body"])
    assert request["model"] == "llava" and len(request["images"]) == 3
    recorder.responses["/api/generate"] = (200, json.dumps({"response": "LIVE"}).encode())
    assert ollama.classify(settings, clip, 2.0)[0] == st.LIVE_ACTION_MODEL
    assert ollama.classify({**settings, "ollama_enabled": False}, clip, 2.0)[0] == st.LIVE_ACTION_MODEL
