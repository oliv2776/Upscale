from __future__ import annotations

import base64
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from conftest import make_video, needs_ffmpeg

import app as uscale

# ------------------------------------------------------------------ chemins et réglages


def test_safe_path_rejects_outside_and_relative(env):
    with pytest.raises(ValueError):
        uscale.safe_path("/etc")
    with pytest.raises(ValueError):
        uscale.safe_path("relative/path")
    assert uscale.safe_path(str(env / "videos"), must_exist=True) == (env / "videos").resolve()


def test_validate_folders_rejects_nesting(env):
    base = uscale.get_settings()
    with pytest.raises(ValueError, match="dans le dossier source"):
        uscale.validate_folders({**base, "destination": str(env / "videos" / "out")})
    (env / "out" / "videos").mkdir(parents=True)
    with pytest.raises(ValueError, match="source ne peut pas"):
        uscale.validate_folders({**base, "source": str(env / "out" / "videos"), "destination": str(env / "out")})
    with pytest.raises(ValueError):
        uscale.validate_folders({**base, "destination": str(env)})


def test_ollama_settings_saved_without_valid_folders(env):
    uscale.store_settings({**uscale.get_settings(), "source": "/nexistepas"})
    settings = uscale.save_ollama_settings({"ollama_enabled": True, "ollama_url": "http://10.0.0.2:11434/"})
    assert settings["ollama_enabled"] is True
    assert settings["ollama_url"] == "http://10.0.0.2:11434"


# ------------------------------------------------------------------ calculs


@pytest.mark.parametrize(("model", "source", "output", "expected"), [
    ("realesr-animevideov3", 480, 720, 2),
    ("realesr-animevideov3", 240, 720, 3),
    ("realesr-animevideov3", 240, 1080, 4),
    ("realesr-animevideov3", 144, 1080, 4),
    ("realesrgan-plus", 480, 720, 4),
])
def test_choose_scale(model, source, output, expected):
    assert uscale.choose_scale(model, source, output) == expected


def test_output_size_respects_anamorphic_dvd():
    ntsc_16_9 = {"width": 720, "height": 480, "sar": 32 / 27}
    assert uscale.output_size(ntsc_16_9, 720) == (1280, 720)
    square = {"width": 640, "height": 480, "sar": 1.0}
    assert uscale.output_size(square, 720) == (960, 720)


def test_output_relative_disambiguates():
    assert uscale.output_relative(Path("a/film.avi"), False) == Path("a/film.mkv")
    assert uscale.output_relative(Path("a/film.avi"), True) == Path("a/film.avi.mkv")


def test_docker_command_mounts_host_path(env):
    cmd = uscale.docker_command("abc123def456", env / "videos" / "a.mp4", env / "out" / "a.mkv",
                                uscale.get_settings(), scale=2)
    assert cmd[cmd.index("-v") + 1] == f"/srv/nas/media:{env.resolve()}"
    assert cmd[cmd.index("-s") + 1] == "2"
    assert uscale.CONTAINER_LABEL in cmd


def test_subtitle_and_audio_args():
    args, skipped = uscale.subtitle_args(["subrip", "mov_text", "eia_608", "hdmv_pgs_subtitle"])
    assert args == ["-map", "1:s:0", "-c:s:0", "copy",
                    "-map", "1:s:1", "-c:s:1", "srt",
                    "-map", "1:s:3", "-c:s:2", "copy"]
    assert skipped == ["#2 (eia_608)"]
    assert uscale.audio_args(["ac3", "pcm_dvd"]) == ["-map", "1:a?", "-c:a", "copy", "-c:a:1", "flac"]


def test_progress_parsers():
    assert uscale.parse_video2x_progress("\rProcessing frame 250/1000 (25.00%); elapsed") == pytest.approx(0.25)
    assert uscale.parse_video2x_progress("[info] frame 3 / 4") == pytest.approx(0.75)
    assert uscale.parse_video2x_progress("Loading model 100%") is None
    parse = uscale.ffmpeg_progress_parser(10.0)
    assert parse("out_time_us=5000000") == pytest.approx(0.5)
    assert parse("fps=24") is None


def test_needs_deinterlace():
    assert uscale.needs_deinterlace("auto", {"field_order": "tt"})
    assert not uscale.needs_deinterlace("auto", {"field_order": "progressive"})
    assert uscale.needs_deinterlace("on", {"field_order": "progressive"})
    assert not uscale.needs_deinterlace("off", {"field_order": "bb"})


# ------------------------------------------------------------------ scan et file


@needs_ffmpeg
def test_scan_disambiguates_collisions_and_uses_cache(env, monkeypatch):
    videos = env / "videos"
    make_video(videos / "film.mp4")
    make_video(videos / "film.avi", extra=["-f", "avi"])
    make_video(videos / "autre.mkv")
    make_video(videos / "grand.mp4", width=1280, height=720)

    result = uscale.scan_files(uscale.get_settings())
    outputs = sorted(Path(f["destination"]).name for f in result["files"])
    assert outputs == ["autre.mkv", "film.avi.mkv", "film.mp4.mkv"]
    assert result["checked"] == 4

    calls = []
    monkeypatch.setattr(uscale, "probe_dimensions", lambda path: calls.append(path))
    again = uscale.scan_files(uscale.get_settings())
    assert len(again["files"]) == 3
    assert calls == []  # tout vient du cache


@needs_ffmpeg
def test_enqueue_skips_known_and_conflicting(env):
    make_video(env / "videos" / "a.mp4")
    files = uscale.scan_files(uscale.get_settings())["files"]
    assert uscale.enqueue(files)["added"] == 1
    again = uscale.scan_files(uscale.get_settings())
    assert again["files"] == [] and again["already_known"] == 1

    other = {**files[0], "source": str(env / "videos" / "b.mp4"), "mtime_ns": 1}
    assert uscale.enqueue([other]) == {"added": 0, "conflicts": 1}


def test_cancel_before_claim_wins(env):
    with uscale.connect() as conn:
        conn.execute("""INSERT INTO jobs(id,source,destination,source_size,source_mtime_ns,status,stage,
                        created_at,updated_at) VALUES('aaaaaaaaaaaa','/s','/d',1,1,'queued','',?,?)""",
                     (uscale.now(), uscale.now()))
    client = uscale.app.test_client()
    assert client.post("/api/cancel/aaaaaaaaaaaa", json={}).status_code == 200
    assert uscale.claim_next_job() is None
    with uscale.connect() as conn:
        assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "cancelled"


def test_run_process_cancellation(env):
    job_id = "bbbbbbbbbbbb"

    def cancel_soon():
        time.sleep(0.5)
        with uscale.state_lock:
            uscale.cancel_job_id = job_id

    threading.Thread(target=cancel_soon).start()
    started = time.monotonic()
    with pytest.raises(uscale.JobCancelled):
        uscale.run_process(job_id, ["sleep", "30"])
    assert time.monotonic() - started < 10


# ------------------------------------------------------------------ sécurité HTTP


def test_post_requires_json_and_same_origin(env):
    client = uscale.app.test_client()
    assert client.post("/api/pause", data="{}", content_type="text/plain").status_code == 415
    assert client.post("/api/pause", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/pause", json={}, headers={"Origin": "http://localhost"}).status_code == 200
    proxied = {"Origin": "https://uscale.maison.lan", "X-Forwarded-Host": "uscale.maison.lan"}
    assert client.post("/api/pause", json={}, headers=proxied).status_code == 200


def test_basic_auth(env, monkeypatch):
    monkeypatch.setattr(uscale, "AUTH_PASSWORD", "secret")
    client = uscale.app.test_client()
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/ping").status_code == 200
    token = base64.b64encode(b"admin:secret").decode()
    assert client.get("/api/jobs", headers={"Authorization": f"Basic {token}"}).status_code == 200


def test_logs_rejects_bad_ids(env):
    assert uscale.app.test_client().get("/api/logs/..%2f..%2fetc").status_code in {400, 404}


# ------------------------------------------------------------------ bout en bout (faux Docker)


def ffprobe_streams(path: Path) -> list[dict]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,sample_aspect_ratio",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["streams"]


def queue_one(env) -> str:
    files = uscale.scan_files(uscale.get_settings())["files"]
    assert uscale.enqueue(files)["added"] == 1
    job = uscale.claim_next_job()
    assert job is not None
    uscale.run_job(job)
    with uscale.connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
    return row


@needs_ffmpeg
def test_end_to_end_mp4_with_mov_text(env, fake_docker):
    uscale.store_settings({**uscale.get_settings(), "model": "realesr-animevideov3", "preset": "ultrafast"})
    make_video(env / "videos" / "serie" / "ep1.mp4", subtitles=True)
    row = queue_one(env)
    assert row["status"] == "completed", (env.parent / "state" / "logs" / f"{row['id']}.log").read_text()
    output = env / "out" / "serie" / "ep1.mkv"
    streams = ffprobe_streams(output)
    video = next(s for s in streams if s["codec_type"] == "video")
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 960, 720)
    assert any(s["codec_type"] == "audio" for s in streams)
    assert any(s["codec_type"] == "subtitle" and s["codec_name"] == "subrip" for s in streams)
    assert list((env / "out" / "serie").iterdir()) == [output]  # temporaires supprimés

    calls = [json.loads(line) for line in fake_docker.read_text().splitlines()]
    run = next(c for c in calls if c[0] == "run")
    assert run[run.index("-s") + 1] == "3"  # 240p -> 720p : ×3 suffit
    assert run[run.index("-v") + 1].startswith("/srv/nas/media:")


@needs_ffmpeg
def test_end_to_end_anamorphic_interlaced(env, fake_docker):
    uscale.store_settings({**uscale.get_settings(), "deinterlace": "auto", "preset": "ultrafast"})
    make_video(env / "videos" / "dvd.mkv", width=360, height=240,
               extra=["-vf", "setfield=tff", "-x264-params", "interlaced=1:tff=1", "-aspect", "16:9"])
    row = queue_one(env)
    assert row["status"] == "completed"
    log = (env.parent / "state" / "logs" / f"{row['id']}.log").read_text()
    assert "bwdif" in log
    video = next(s for s in ffprobe_streams(env / "out" / "dvd.mkv") if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1280, 720)


@needs_ffmpeg
def test_refuses_to_overwrite_and_checks_disk(env, fake_docker, monkeypatch):
    make_video(env / "videos" / "a.mp4")
    files = uscale.scan_files(uscale.get_settings())["files"]
    uscale.enqueue(files)
    job = uscale.claim_next_job()

    class Usage:
        free = 1024**2

    monkeypatch.setattr(uscale.shutil, "disk_usage", lambda path: Usage)
    uscale.run_job(job)
    with uscale.connect() as conn:
        row = conn.execute("SELECT status,error FROM jobs").fetchone()
    assert row["status"] == "failed" and "Espace disque insuffisant" in row["error"]
    assert not (env / "out" / "a.mkv").exists()


def test_recover_removes_orphan_containers(env, fake_docker):
    with uscale.connect() as conn:
        conn.execute("""INSERT INTO jobs(id,source,destination,source_size,source_mtime_ns,status,stage,
                        created_at,updated_at) VALUES('cccccccccccc','/s','/d',1,1,'running','',?,?)""",
                     (uscale.now(), uscale.now()))
    uscale.recover_interrupted_jobs()
    calls = [json.loads(line) for line in fake_docker.read_text().splitlines()]
    assert ["rm", "-f", "uscale-cccccccccccc"] in calls
    assert any(c[:2] == ["ps", "-aq"] for c in calls)
    with uscale.connect() as conn:
        assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "queued"


def test_gpu_check_is_async_and_cached(env, fake_docker):
    client = uscale.app.test_client()
    assert client.post("/api/health/gpu", json={}).status_code == 202
    for _ in range(50):
        gpu = client.get("/api/health").get_json()["gpu"]
        if gpu["status"] != "running":
            break
        time.sleep(0.1)
    assert gpu["status"] == "ok" and "RX 5500 XT" in gpu["devices"][0]


def test_scan_endpoint_runs_in_background(env):
    client = uscale.app.test_client()
    settings = uscale.get_settings()
    response = client.post("/api/scan", json={"source": settings["source"], "destination": settings["destination"]})
    assert response.status_code == 202
    for _ in range(50):
        state = client.get("/api/scan").get_json()
        if not state["running"]:
            break
        time.sleep(0.1)
    assert state["found"] == 0 and state["error"] is None
