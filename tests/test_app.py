from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from conftest import calls, make_video, needs_ffmpeg

from upscale import config, converter, db, gpu, media, models, net, notify, ollama, pipeline, runner, scanner, updates, worker
from upscale import settings as st
from upscale import web as web_module
from upscale.web import app

# ================================================================== réglages


def test_safe_path_rejects_outside_and_relative(env):
    with pytest.raises(ValueError):
        st.safe_path("/etc")
    with pytest.raises(ValueError):
        st.safe_path("relative/path")
    assert st.safe_path(str(env / "videos"), must_exist=True) == (env / "videos").resolve()


def test_validate_folders_accepts_parent_and_destination_inside(env):
    base = st.get_settings()
    videos = env / "videos"
    out = videos / "out"
    sources, destination = st.validate_folders({**base, "sources": [str(videos)], "destination": str(out)})
    assert sources == [videos.resolve()]
    assert destination == out.resolve()
    (env / "out2" / "videos").mkdir(parents=True)
    with pytest.raises(ValueError, match="source ne peut pas"):
        st.validate_folders({**base, "sources": [str(env / "out2" / "videos")],
                             "destination": str(env / "out2")})
    with pytest.raises(ValueError):
        st.validate_folders({**base, "destination": str(env)})
    (videos / "Films").mkdir()
    sources, _ = st.validate_folders({**base, "sources": [str(videos), str(videos / "Films")]})
    assert sources == [videos.resolve()]
    sources, _ = st.validate_folders({**base, "sources": [str(videos), str(videos)]})
    assert sources == [videos.resolve()]
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


def test_boolean_validation_is_strict(env):
    assert st.save_settings({"include_subfolders": "false"})["include_subfolders"] is False
    assert st.save_settings({"include_subfolders": "true"})["include_subfolders"] is True
    assert st.save_settings({"replace_original": 0})["replace_original"] is False
    for bad in ("peut-être", 2, None):
        with pytest.raises(ValueError):
            st.save_settings({"replace_original": bad})


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
def test_probe_dimensions_ignores_attached_cover(monkeypatch):
    monkeypatch.setattr(media, "ffprobe_json", lambda *a, **k: {"streams": [
        {"codec_name": "mjpeg", "width": 200, "height": 150, "disposition": {"attached_pic": 1}},
        {"codec_name": "h264", "width": 1920, "height": 1080, "disposition": {}},
    ]})
    assert media.probe_dimensions(Path("/data/film.mkv")) == (1920, 1080)


def test_probe_cache_is_invalidated_when_version_changes(env):
    with db.connect() as conn:
        conn.execute("INSERT INTO probe_cache VALUES('/data/vieux.mkv',1,1,320,180,?)", (db.now(),))
    db.set_state("probe_cache_version", "1")
    db.init_db()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM probe_cache").fetchone()[0] == 0
    assert db.get_state("probe_cache_version") == str(config.PROBE_CACHE_VERSION)


@needs_ffmpeg
def test_scan_reports_resolution_histogram(env):
    videos = env / "videos"
    make_video(videos / "sd.mkv", width=160, height=120)
    make_video(videos / "dvd.mkv", width=640, height=480)
    make_video(videos / "hd.mkv", width=1280, height=720)
    result = scanner.scan_files(st.get_settings())
    assert result["resolutions"] == {"under_360": 1, "360_480": 1, "over_480": 1, "unknown": 0}
    assert len(result["files"]) == 2  # 120p et 480p sont sous la limite de 480p


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
def test_scan_parent_with_subfolders_and_destination_inside(env):
    videos = env / "videos"
    make_video(videos / "Films" / "film.mkv", width=160, height=120)
    make_video(videos / "Series" / "episode.mkv", width=160, height=120)
    make_video(videos / "out" / "deja_traite.mkv", width=160, height=120)
    settings = st.get_settings()
    settings.update({"sources": [str(videos)], "source": str(videos),
                     "destination": str(videos / "out"), "include_subfolders": True})
    result = scanner.scan_files(settings)
    names = sorted(Path(item["source"]).name for item in result["files"])
    assert names == ["episode.mkv", "film.mkv"]


@needs_ffmpeg
def test_scan_non_recursive_ignores_subfolders(env):
    videos = env / "videos"
    make_video(videos / "racine.mkv", width=160, height=120)
    make_video(videos / "Sous" / "enfant.mkv", width=160, height=120)
    settings = st.get_settings()
    settings.update({"sources": [str(videos)], "source": str(videos),
                     "destination": str(env / "out"), "include_subfolders": False})
    assert [Path(item["source"]).name for item in scanner.scan_files(settings)["files"]] == ["racine.mkv"]


def test_save_dedupes_nested_sources(env):
    videos = env / "videos"
    (videos / "Films").mkdir()
    saved = st.save_settings({"sources": [str(videos / "Films"), str(videos)],
                              "destination": str(env / "out")})
    assert saved["sources"] == [str(videos.resolve())]


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


def test_jobs_all_includes_previews(env):
    insert_job("aaaaaaaaaaa1", status="queued", position=1.0)
    insert_job("bbbbbbbbbbb1", status="queued", position=2.0, kind="preview", priority=100)
    data = app.test_client().get("/api/jobs?kind=all").get_json()
    assert {job["id"] for job in data["jobs"]} == {"aaaaaaaaaaa1", "bbbbbbbbbbb1"}
    assert data["preview_counts"]["queued"] == 1
    assert [job["id"] for job in data["jobs"]][0] == "bbbbbbbbbbb1"  # aperçu prioritaire
    previews = app.test_client().get("/api/jobs?kind=preview").get_json()["jobs"]
    assert [job["id"] for job in previews] == ["bbbbbbbbbbb1"]


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


def test_move_to_index(env):
    for index, job_id in enumerate(["aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3"]):
        insert_job(job_id, position=float(index))
    client = app.test_client()
    assert client.post("/api/jobs/aaaaaaaaaaa3/move", json={"to": "index", "index": 0}).status_code == 200
    assert [job["id"] for job in client.get("/api/jobs?status=queued").get_json()["jobs"]] == \
        ["aaaaaaaaaaa3", "aaaaaaaaaaa1", "aaaaaaaaaaa2"]
    assert client.post("/api/jobs/aaaaaaaaaaa1/move", json={"to": "index", "index": 2}).status_code == 200
    assert [job["id"] for job in client.get("/api/jobs?status=queued").get_json()["jobs"]] == \
        ["aaaaaaaaaaa3", "aaaaaaaaaaa2", "aaaaaaaaaaa1"]
    assert client.post("/api/jobs/aaaaaaaaaaa1/move", json={"to": "index", "index": "x"}).status_code == 400


def test_move_relative_before_and_after(env):
    for index, job_id in enumerate(["aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3"]):
        insert_job(job_id, position=float(index))
    client = app.test_client()
    assert client.post("/api/jobs/aaaaaaaaaaa3/move",
                       json={"to": "before", "id": "aaaaaaaaaaa1"}).status_code == 200
    assert [job["id"] for job in client.get("/api/jobs?status=queued").get_json()["jobs"]] == \
        ["aaaaaaaaaaa3", "aaaaaaaaaaa1", "aaaaaaaaaaa2"]
    assert client.post("/api/jobs/aaaaaaaaaaa3/move",
                       json={"to": "after", "id": "aaaaaaaaaaa2"}).status_code == 200
    assert [job["id"] for job in client.get("/api/jobs?status=queued").get_json()["jobs"]] == \
        ["aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3"]
    assert client.post("/api/jobs/aaaaaaaaaaa1/move",
                       json={"to": "before", "id": "ffffffffffff"}).status_code == 400


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
    (config.thumbnail_dir() / "orphelin.jpg").write_bytes(b"x")
    with db.connect() as conn:
        conn.execute("INSERT INTO probe_cache VALUES('/nexiste/pas.mkv',1,1,10,10,?)", (db.now(),))
    for index in range(3):
        job_id = f"ccccccccccc{index}"
        insert_job(job_id, status="completed", kind="preview")
        (config.preview_dir() / job_id).mkdir(parents=True)
        db.set_job(job_id, updated_at=f"2026-01-0{index + 1}T00:00:00+00:00")
    result = worker.maintenance()
    assert result == {"logs": 1, "probe_cache": 1, "previews": 2, "thumbnails": 1,
                      "owners": 0, "trash": 0}
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


def test_post_requires_json_and_same_origin(env, monkeypatch):
    client = app.test_client()
    assert client.post("/api/pause", data="{}", content_type="text/plain").status_code == 415
    assert client.post("/api/pause", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/pause", json={}, headers={"Origin": "http://localhost"}).status_code == 200
    # DNS rebinding : Host et Origin identiques, mais nom non autorisé.
    rebinding = {"Origin": "http://evil.example:27000"}
    assert client.post("/api/pause", json={}, base_url="http://evil.example:27000",
                       headers=rebinding).status_code == 403
    # Accès direct par IP (LAN) : autorisé.
    assert client.post("/api/pause", json={}, base_url="http://192.168.1.10:27000",
                       headers={"Origin": "http://192.168.1.10:27000"}).status_code == 200
    # Reverse proxy : le nom public doit figurer dans UPSCALE_TRUSTED_HOSTS.
    monkeypatch.setattr(config, "TRUSTED_HOSTS", {"upscale.maison.lan"})
    proxied = {"Origin": "https://upscale.maison.lan"}
    assert client.post("/api/pause", json={}, base_url="https://upscale.maison.lan",
                       headers=proxied).status_code == 200
    assert client.post("/api/pause", json={}, base_url="https://upscale.maison.lan",
                       headers={"Origin": "https://evil.example"}).status_code == 403


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


def test_anonymous_mode_blocks_mutations(env, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_ANONYMOUS", False)
    client = app.test_client()
    assert client.get("/api/config").status_code == 200
    response = client.post("/api/pause", json={})
    assert response.status_code == 403
    assert "UPSCALE_PASSWORD" in response.get_json()["error"]
    monkeypatch.setattr(config, "ALLOW_ANONYMOUS", True)
    assert client.post("/api/pause", json={}).status_code == 200


def test_config_reports_auth_state(env, monkeypatch):
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOW_ANONYMOUS", False)
    data = app.test_client().get("/api/config").get_json()
    assert data["auth"] == {"password_set": False, "anonymous_allowed": False, "env_forced": False}


def test_lan_mode_can_be_enabled_from_ui(env, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_ANONYMOUS", False)
    web_module._lan_cache.update(value=None, at=0.0)
    client = app.test_client()
    assert client.post("/api/pause", json={}).status_code == 403
    # Le réglage est protégé par la même vérification d'origine que le reste de l'API.
    assert client.post("/api/auth/lan-mode", json={"enabled": True},
                       headers={"Origin": "http://evil.example"}).status_code == 403
    response = client.post("/api/auth/lan-mode", json={"enabled": True})
    assert response.status_code == 200
    assert response.get_json()["auth"]["anonymous_allowed"] is True
    assert client.post("/api/pause", json={}).status_code == 200
    assert db.get_state("lan_mode") == "1"
    assert client.post("/api/auth/lan-mode", json={"enabled": False}).status_code == 200
    assert client.post("/api/pause", json={}).status_code == 403


def test_lan_mode_is_disabled_by_password_or_env(env, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_ANONYMOUS", True)
    assert app.test_client().post("/api/auth/lan-mode", json={"enabled": False}).status_code == 409
    monkeypatch.setattr(config, "ALLOW_ANONYMOUS", False)
    monkeypatch.setattr(config, "AUTH_PASSWORD", "secret")
    token = base64.b64encode(b"admin:secret").decode()
    response = app.test_client().post("/api/auth/lan-mode", json={"enabled": True},
                                      headers={"Authorization": f"Basic {token}"})
    assert response.status_code == 409


def test_update_check_reads_github_release(env, http_server, monkeypatch):
    base, recorder = http_server
    recorder.responses["/latest"] = (200, json.dumps({
        "tag_name": "v9.9.9", "html_url": "https://example.test/release", "body": "notes"}).encode())
    monkeypatch.setattr(updates, "API_URL", f"{base}/latest")
    updates.reset_cache()
    data = updates.state(force=True)
    assert data["latest"] == "9.9.9" and data["update_available"] is True
    assert data["url"] == "https://example.test/release" and data["notes"] == "notes"
    assert data["error"] is None
    assert app.test_client().get("/api/update?refresh=1").get_json()["latest"] == "9.9.9"


def test_update_check_reports_404_and_uses_cache(env, http_server, monkeypatch):
    base, recorder = http_server
    recorder.responses["/latest"] = (404, b"{}")
    monkeypatch.setattr(updates, "API_URL", f"{base}/latest")
    updates.reset_cache()
    first = updates.state(force=True)
    assert "Aucune release" in first["error"]
    recorder.responses["/latest"] = (200, json.dumps({"tag_name": "v9.9.9"}).encode())
    assert updates.state() == first  # cache de 30 minutes
    assert updates.state(force=True)["latest"] == "9.9.9"


def test_update_version_parsing():
    assert updates.parse_version("v2.10.0") == (2, 10, 0)
    assert updates.parse_version("2.9.1") == (2, 9, 1)
    assert updates.parse_version("dev") is None


def test_security_headers(env):
    client = app.test_client()
    response = client.get("/api/ping")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert client.get("/").headers["Cache-Control"] == "no-cache"
    assert client.get("/static/app.js").headers["Cache-Control"] == "no-cache"


def test_auth_failures_are_throttled(env, monkeypatch):
    monkeypatch.setattr(config, "AUTH_PASSWORD", "secret")
    client = app.test_client()
    for _ in range(web_module.AUTH_MAX_FAILURES):
        assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs").status_code == 429


def test_cancel_finished_job_is_conflict(env):
    with db.connect() as conn:
        conn.execute("""INSERT INTO jobs (id,source,destination,source_size,source_mtime_ns,status,stage,
                                          created_at,updated_at)
                        VALUES('bbbbbbbbbbbb','/data/a.mkv','/data/out/a.mkv',1,1,'completed','Terminé',
                               '2026-01-01','2026-01-01')""")
    client = app.test_client()
    assert client.post("/api/jobs/bbbbbbbbbbbb/cancel", json={}).status_code == 409
    assert client.post("/api/jobs/cccccccccccc/cancel", json={}).status_code == 404


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


TINY_PARAM = b"7767517\n2 2\nInput in0 0 1 in0\nConvolution conv0 1 1 in0 out0 0=3\n"


def import_model(client, label="Peau réaliste 2x", scale=2, param=TINY_PARAM):
    return client.post("/api/models", headers={"X-Upscale-Upload": "1"}, content_type="multipart/form-data", data={
        "label": label, "scale": str(scale), "tile": "0",
        "param": (io.BytesIO(param), "modele.param"), "bin": (io.BytesIO(b"\0" * 4096), "modele.bin")})


def test_custom_model_management(env):
    client = app.test_client()
    bad = import_model(client, param=b"pas un modele")
    assert bad.status_code == 400 and "ncnn" in bad.get_json()["error"]
    no_header = client.post("/api/models", content_type="multipart/form-data", data={"label": "x"})
    assert no_header.status_code == 415  # import impossible depuis un site tiers (CSRF)
    created = import_model(client)
    assert created.status_code == 201
    model_id = created.get_json()["model"]["id"]
    assert model_id == "perso-peau-realiste-2x"
    listed = client.get("/api/config").get_json()["models"]
    assert listed[model_id]["custom"] is True and listed[model_id]["scales"] == [2]
    assert media.choose_scale(model_id, 240, 720) == 2  # facteur unique du modèle
    assert import_model(client).get_json()["model"]["id"] == "perso-peau-realiste-2x-2"
    assert client.post(f"/api/models/{model_id}/update", json={"label": "Peau v2", "scale": 4, "tile": 256}).status_code == 200
    assert models.get(model_id)["label"] == "Peau v2" and models.get(model_id)["tile"] == 256
    assert client.post(f"/api/models/{model_id}/update", json={"label": "x", "scale": 5}).status_code == 400
    st.save_settings({"model": model_id})
    assert client.post(f"/api/models/{model_id}/delete", json={}).status_code == 400  # encore utilisé
    st.save_settings({"model": "realesrgan-plus"})
    assert client.post(f"/api/models/{model_id}/delete", json={}).status_code == 200
    with pytest.raises(ValueError):
        st.save_settings({"model": model_id})
    assert client.post("/api/models/..%2f..%2fetc/delete", json={}).status_code in {400, 404}
    legacy = models.models_dir() / "perso-ancien"  # modèle importé par la version 2.6
    legacy.mkdir()
    (legacy / "realesrgan-x4plus-perso.param").write_bytes(TINY_PARAM)
    (legacy / "realesrgan-x4plus-perso.bin").write_bytes(b"\0" * 4096)
    (legacy / "meta.json").write_text(json.dumps({"id": "perso-ancien", "label": "Ancien", "scale": 2, "tile": 0,
                                                  "param_size": 1, "bin_size": 4096}))
    assert models.get("perso-ancien")["scale"] == 2
    assert (legacy / "modele.param").exists() and not (legacy / "realesrgan-x4plus-perso.param").exists()


@needs_ffmpeg
def test_custom_model_end_to_end_and_preview(env, fake_docker, monkeypatch):
    monkeypatch.setattr(config, "CUSTOM_SEGMENT_SECONDS", 1)
    client = app.test_client()
    model_id = import_model(client).get_json()["model"]["id"]
    st.save_settings({"model": model_id, "render": "custom", "ai_strength": 80, "grain": "medium"})
    source = make_video(env / "videos" / "scene.mp4", seconds=2)
    [row] = process_all(env)
    assert row["status"] == "completed" and row["warning"] is None, job_log(row)
    ncnn_calls = [c for c in calls(fake_docker) if c[0] == "ncnn"]
    assert len(ncnn_calls) == 2 and ncnn_calls[0][ncnn_calls[0].index("-z") + 1] == "2"  # 2 segments de 1 s
    assert "models" in ncnn_calls[0][ncnn_calls[0].index("-m") + 1]  # exigé par le moteur
    assert not any(c[:2] == ["docker", "run"] for c in calls(fake_docker))  # Video2X inutile ici
    out = env / "out" / "scene.mkv"
    assert video_stream(out)["height"] == 720 and abs(media.probe_source(out)["duration"] - 2.0) < 0.3

    response = client.post("/api/preview", json={"source": str(source), "duration": 2, "models": ["realesrgan-plus"],
                                                 "peau_normale": False})
    assert response.status_code == 202
    job = worker.claim_next_job()
    pipeline.run_job(job)
    manifest = client.get("/api/previews").get_json()["previews"][0]["manifest"]
    assert [v["id"] for v in manifest["variants"]] == ["original", "config", "realesrgan-plus"]
    label = manifest["variants"][1]["label"]
    assert "Peau réaliste 2x (personnalisé)" in label and "Personnalisé (80 % IA, grain medium)" in label
    assert "HEVC CRF 16" in label
    log = (config.log_dir() / f"{job['id']}.log").read_text()
    assert "blend=all_expr='A*0.80+B*0.20'" in log and "libx265" in log  # rendu et encodeur appliqués


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
    assert "Emby" in notify.refresh_media_server({"media_type": "emby", "media_url": base, "media_token": "e"})
    assert recorder.received[-1]["path"] == "/Library/Refresh"
    assert recorder.received[-1]["headers"]["X-Emby-Token"] == "e"
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


# ---------------------------------------------------------------- installation de modèles (OpenModelDB)

PNNX_PARAM = "7767517\n3 3\nInput in0 0 1 in0\nConvolution conv_0 1 1 in0 1 0=3\nPixelShuffle ps_1 1 1 1 out0 0=2\n"


def test_normalize_param_names_engine_blobs():
    text = models.normalize_param(PNNX_PARAM)
    assert "Input in0 0 1 data" in text and "conv_0 1 1 data 1 0=3" in text and "ps_1 1 1 1 output 0=2" in text
    assert models.normalize_param(text) == text  # déjà conforme (chaiNNer) : inchangé
    clash = "7767517\n3 3\nInput input 0 1 input\nConvolution c 1 1 input data\nSigmoid s 1 1 data out\n"
    renamed = models.normalize_param(clash)
    assert "c 1 1 data data_interne" in renamed and "s 1 1 data_interne output" in renamed
    with pytest.raises(ValueError):  # deux sorties
        models.normalize_param("7767517\n3 4\nInput in0 0 1 in0\nSplit sp 1 2 in0 a b\n")
    with pytest.raises(ValueError):
        models.normalize_param("pas ncnn\n")


def test_direct_download_urls():
    drive = converter.direct_url("https://drive.google.com/file/d/1CX9tpoS-OtU/view?usp=drive_link")
    assert drive == "https://drive.usercontent.google.com/download?id=1CX9tpoS-OtU&export=download&confirm=t"
    assert converter.direct_url("https://drive.google.com/drive/folders/1eHak") is None
    assert converter.direct_url("https://mega.nz/file/abc") is None
    assert converter.direct_url("https://github.com/u/r/blob/main/m.pth") == "https://github.com/u/r/raw/main/m.pth"
    assert converter.direct_url("https://huggingface.co/u/r/blob/main/m.pth") == "https://huggingface.co/u/r/resolve/main/m.pth"
    assert converter.direct_url("file:///etc/passwd") is None
    assert converter.openmodeldb_id("https://openmodeldb.info/models/2x-LiveActionV1-SPAN?x=1") == "2x-LiveActionV1-SPAN"
    assert converter.openmodeldb_id("2x-LiveActionV1-SPAN") == "2x-LiveActionV1-SPAN"
    with pytest.raises(converter.InstallError):
        converter.openmodeldb_id("../../etc/passwd")
    ranked = converter.ordered_resources([{"platform": "onnx"}, {"platform": "pytorch"}, {"platform": "tensorrt"},
                                          {"platform": "ncnn"}])
    assert [r["platform"] for r in ranked] == ["ncnn", "pytorch", "onnx"]


def test_private_model_addresses_are_refused(env, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_PRIVATE_MODEL_URLS", False)
    with pytest.raises(net.UnsafeURLError):
        net.validate_url("http://127.0.0.1/modele.pth")
    with pytest.raises(net.UnsafeURLError):
        net.validate_url("http://169.254.169.254/latest/meta-data")
    with pytest.raises(converter.InstallError):
        converter.download({}, "http://127.0.0.1:9/modele.pth", env / "telechargement")
    # Redirection vers une adresse privée : refusée avant d'être suivie.
    with pytest.raises(net.UnsafeURLError):
        net._CheckedRedirect(False).redirect_request(None, None, 302, "", {}, "http://10.0.0.1/modele.pth")
    monkeypatch.setattr(config, "ALLOW_PRIVATE_MODEL_URLS", True)
    assert net.validate_url("http://127.0.0.1/modele.pth", allow_private=True)


def test_drop_privileges_builds_setpriv_command(env, monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "modele.pth").write_text("x")
    monkeypatch.setattr(converter, "_nobody", lambda: (65534, 65534))
    monkeypatch.setattr(converter.shutil, "which", lambda name: "/usr/bin/setpriv")
    monkeypatch.setattr(converter.os, "chown", lambda *args, **kwargs: None)
    command = converter.drop_privileges(["python", "-I", "convert.py"], work)
    assert command == ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--",
                       "python", "-I", "convert.py"]


def test_drop_privileges_is_noop_when_not_root(env, monkeypatch):
    monkeypatch.setattr(converter.os, "geteuid", lambda: 1000, raising=False)
    command = ["python", "-I", "script.py"]
    assert converter.drop_privileges(command, env / "work") == command


def test_converter_environment_does_not_leak_secrets(env, monkeypatch):
    monkeypatch.setenv("UPSCALE_PASSWORD", "s3cret")
    values = converter.converter_env()
    assert "UPSCALE_PASSWORD" not in values
    assert values["PYTHONIOENCODING"] == "utf-8"


def omdb_entry(base: str, payload: bytes, **extra):
    return {"name": "2xTestLive_SPAN", "author": "jcj83429", "license": "CC-BY-NC-SA-4.0", "architecture": "span",
            "scale": 2, "inputChannels": 3, "outputChannels": 3,
            "resources": [{"platform": "pytorch", "type": "pth", "size": len(payload),
                           "sha256": hashlib.sha256(payload).hexdigest(),
                           "urls": ["https://drive.google.com/drive/folders/inutilisable", f"{base}/files/2x_live.pth"]}],
            **extra}


@needs_ffmpeg
def test_install_from_openmodeldb_link(env, fake_converter, http_server, monkeypatch):
    base, recorder = http_server
    monkeypatch.setattr(config, "OPENMODELDB_DATA", f"{base}/models")
    payload = b"PK-faux-pth" * 500
    recorder.responses["/models/2x-TestLive-SPAN.json"] = (200, json.dumps(omdb_entry(base, payload)).encode())
    recorder.responses["/files/2x_live.pth"] = (200, payload)
    client = app.test_client()
    response = client.post("/api/models/install", json={"source": "https://openmodeldb.info/models/2x-TestLive-SPAN"})
    assert response.status_code == 202, response.get_json()
    install_id = response.get_json()["install"]["id"]
    converter.process_pending()
    task = client.get(f"/api/models/installs/{install_id}").get_json()["install"]
    assert task["state"] == "done", task
    meta = models.get(task["model_id"])
    assert meta["label"] == "2xTestLive_SPAN" and meta["scale"] == 2 and meta["license"] == "CC-BY-NC-SA-4.0"
    assert meta["page"] == "https://openmodeldb.info/models/2x-TestLive-SPAN" and meta["architecture"] == "SPAN"
    assert meta["verified"]["psnr"] >= converter.MIN_PSNR  # moteur comparé au modèle d'origine
    param = (Path(meta["folder"]) / f"{models.NCNN_NAME}.param").read_text()
    assert "Input in0 0 1 data" in param and " output " in param  # noms attendus par le moteur
    assert any("dossier Google Drive" in line for line in task["log"])  # lien inutilisable ignoré
    listed = client.get("/api/models").get_json()
    assert listed["models"][meta["id"]]["license"] == "CC-BY-NC-SA-4.0"
    assert listed["installs"][0]["id"] == install_id and "_upload" not in listed["installs"][0]
    assert listed["suggestions"][0]["id"] == "2x-LiveActionV1-SPAN"
    ncnn = [c for c in calls(fake_converter) if c[0] == "ncnn"]
    assert ncnn and ncnn[-1][ncnn[-1].index("-z") + 1] == "2"
    st.save_settings({"model": meta["id"]})  # utilisable tout de suite
    assert not list(models.models_dir().glob(".installation-*"))  # dossier de travail nettoyé


@needs_ffmpeg
def test_install_rejects_tampered_or_unsupported_models(env, fake_converter, http_server, monkeypatch):
    base, recorder = http_server
    monkeypatch.setattr(config, "OPENMODELDB_DATA", f"{base}/models")
    payload = b"contenu" * 400
    entry = omdb_entry(base, payload)
    entry["resources"][0]["sha256"] = "0" * 64
    recorder.responses["/models/2x-Modifie.json"] = (200, json.dumps(entry).encode())
    recorder.responses["/files/2x_live.pth"] = (200, payload)
    recorder.responses["/models/8x-Enorme.json"] = (200, json.dumps({**omdb_entry(base, payload), "scale": 8}).encode())
    recorder.responses["/models/Absent.json"] = (404, b"{}")
    client = app.test_client()
    ids = [client.post("/api/models/install", json={"source": name}).get_json()["install"]["id"]
           for name in ("2x-Modifie", "8x-Enorme", "Absent")]
    converter.process_pending()
    tampered, huge, missing = (converter.get_install(i) for i in ids)
    assert tampered["state"] == "error" and "SHA-256" in tampered["message"]
    assert huge["state"] == "error" and "×8" in huge["message"]
    assert missing["state"] == "error" and "introuvable" in missing["message"]
    assert client.post("/api/models/install", json={"source": "pas un lien/../x"}).status_code == 400
    assert not models.list_models()


@needs_ffmpeg
def test_upload_pth_onnx_and_ncnn_zip(env, fake_converter, http_server):
    client = app.test_client()
    upload = lambda name, data, **form: client.post(  # noqa: E731
        "/api/models", headers={"X-Upscale-Upload": "1"}, content_type="multipart/form-data",
        data={"tile": "0", **form, "file": (io.BytesIO(data), name)})
    pth = upload("4x_Peau.pth", b"x" * 5000, label="Peau ×4")
    onnx_no_scale = upload("modele.onnx", b"x" * 5000)
    onnx = upload("modele.onnx", b"x" * 5000, scale="3", label="Onnx x3")
    gelu = upload("2x_gelu.pth", b"x" * 5000)
    dat = upload("4x_DAT.pth", b"x" * 5000)
    assert upload("notes.txt", b"x").status_code == 400
    ids = [r.get_json()["install"]["id"] for r in (pth, onnx_no_scale, onnx, gelu, dat)]
    assert all(r.status_code == 202 for r in (pth, onnx_no_scale, onnx, gelu, dat))
    converter.process_pending()
    done_pth, no_scale, done_onnx, failed_gelu, failed_dat = (converter.get_install(i) for i in ids)
    assert done_pth["state"] == "done" and models.get(done_pth["model_id"])["scale"] == 4
    assert models.get(done_pth["model_id"])["label"] == "Peau ×4"
    assert no_scale["state"] == "error" and "facteur" in no_scale["message"]
    assert done_onnx["state"] == "done" and models.get(done_onnx["model_id"])["scale"] == 3
    assert models.get(done_onnx["model_id"])["verified"].get("psnr") is None  # pas de référence PyTorch
    assert failed_gelu["state"] == "error" and "Gelu" in failed_gelu["message"]  # couche inconnue du moteur
    assert failed_dat["state"] == "error" and "DAT" in failed_dat["message"]
    assert not list(models.models_dir().glob(".envoi-*"))  # fichiers envoyés supprimés

    # Archive ncnn prête à l'emploi (lien direct) : aucune conversion.
    base, recorder = http_server
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("modele/x2.param", PNNX_PARAM)
        z.writestr("modele/x2.bin", b"\0" * 4096)
        z.writestr("../../evasion.param", "ignoré : un seul .param attendu")
    recorder.responses["/ncnn.zip"] = (200, archive.getvalue())
    install = client.post("/api/models/install", json={"source": f"{base}/ncnn.zip", "scale": 2}).get_json()["install"]
    converter.process_pending()
    assert converter.get_install(install["id"])["state"] == "error"  # deux .param : ambigu
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("modele/x2.param", PNNX_PARAM)
        z.writestr("modele/x2.bin", b"\0" * 4096)
    recorder.responses["/ncnn.zip"] = (200, archive.getvalue())
    before = len([c for c in calls(fake_converter) if c[0] == "converter"])
    install = client.post("/api/models/install", json={"source": f"{base}/ncnn.zip", "scale": 2,
                                                       "label": "Zip ncnn"}).get_json()["install"]
    converter.process_pending()
    task = converter.get_install(install["id"])
    assert task["state"] == "done", task
    assert len([c for c in calls(fake_converter) if c[0] == "converter"]) == before
    assert not (env.parent / "evasion.param").exists()


def test_converter_environment_is_installed_once(env, monkeypatch):
    monkeypatch.setattr(config, "CONVERTER_PYTHON", "")
    commands = []

    def fake_stream(task, command, **kwargs):
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            python = Path(command[3]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\nexit 0\n")
            python.chmod(0o755)
        return 0, []

    monkeypatch.setattr(converter, "stream", fake_stream)
    task = {"id": "t", "log": []}
    python = converter.ensure_converter(task)
    assert python == config.converter_dir() / "bin" / "python"
    pip_torch = next(c for c in commands if "--index-url" in c)
    assert pip_torch[pip_torch.index("--index-url") + 1] == config.TORCH_INDEX
    assert any(p.startswith("torch==") for p in pip_torch)
    assert any(any(p.startswith("spandrel==") for p in c) and any(p.startswith("pnnx==") for p in c) for c in commands)
    commands.clear()
    assert converter.ensure_converter(task) == python and not commands  # déjà prêt : rien à installer
    assert converter.converter_status()["installed"] is True


def test_native_esrgan_writer_matches_pytorch(tmp_path):
    """Écriture ncnn directe d'ESRGAN, comparée à PyTorch (si torch, spandrel et ncnn sont installés)."""
    torch = pytest.importorskip("torch")
    ncnn = pytest.importorskip("ncnn")
    rrdb = pytest.importorskip("spandrel.architectures.ESRGAN.__arch.RRDB")
    import importlib.util

    import numpy as np
    spec = importlib.util.spec_from_file_location("convert_model", Path(converter.CONVERT_SCRIPT))
    convert_model = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(convert_model)
    torch.manual_seed(0)
    for scale, plus in ((2, False), (4, True)):
        net = rrdb.RRDBNet(num_filters=16, num_blocks=2, scale=scale, plus=plus).eval()
        descriptor = type("D", (), {"model": net, "architecture": type("A", (), {"name": "ESRGAN"})})
        work = tmp_path / f"x{scale}"
        work.mkdir()
        convert_model.write_native(descriptor, work)
        models.normalize_param((work / "model.param").read_text())  # format accepté à l'import
        sample = torch.rand(1, 3, 20, 26)
        with torch.no_grad():
            expected = net(sample)[0].numpy()
        engine = ncnn.Net()
        engine.opt.use_fp16_storage = engine.opt.use_fp16_packed = engine.opt.use_fp16_arithmetic = False
        engine.load_param(str(work / "model.param"))
        engine.load_model(str(work / "model.bin"))
        extractor = engine.create_extractor()
        pixels = np.ascontiguousarray(sample[0].numpy())  # référence gardée : ncnn.Mat ne copie pas
        extractor.input("data", ncnn.Mat(pixels))
        _, out = extractor.extract("output")
        assert np.abs(np.array(out) - expected).max() < 1e-4
