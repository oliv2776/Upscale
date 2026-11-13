"""Réglages, validation et commandes FFmpeg."""
from __future__ import annotations

from pathlib import Path

import pytest
from helpers import (
    insert_job,
)

from upscale import config, db, media, pipeline, runner
from upscale import settings as st
from upscale.web import app


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


def test_hevc_default_and_migration(env):
    assert st.DEFAULT_SETTINGS["codec"] == "hevc"
    payload = {k: v for k, v in st.get_settings().items() if k != "settings_version"}
    db.store_settings_payload({**payload, "codec": "h264"})  # réglages d'une version précédente
    st.migrate()
    assert st.get_settings()["codec"] == "hevc"
    st.save_settings({"codec": "h264"})  # choix explicite après migration : respecté
    st.migrate()
    assert st.get_settings()["codec"] == "h264"


def test_deblock_and_gpu_denoise_filters(monkeypatch):
    info = {"field_order": "progressive"}
    assert media.prefilters({"deblock": "strong", "denoise": "medium"}, info) == [
        media.DEBLOCK_FILTERS["strong"], media.DENOISE_FILTERS["medium"]]
    assert st.validate_value("deblock", "light") == "light"
    strong = media.prefilters({"denoise": "strong"}, info)
    assert strong == [media.NLMEANS_CPU]            # l'empreinte de reprise garde le nom « processeur »
    monkeypatch.setitem(media._vulkan_denoise, "ok", True)
    command = media.segment_command(Path("a.mkv"), Path("seg_%05d.mkv"), strong, 30)
    assert "nlmeans_vulkan" in " ".join(command) and command.index("-init_hw_device") < command.index("-i")
    monkeypatch.setitem(media._vulkan_denoise, "ok", False)
    command = media.segment_command(Path("a.mkv"), Path("seg_%05d.mkv"), strong, 30)
    assert "nlmeans_vulkan" not in " ".join(command) and "-init_hw_device" not in command
    pre, _, args = media.encoder_args({"codec": "av1_vaapi", "crf": 16})
    assert args[:2] == ["-c:v", "av1_vaapi"] and "-vaapi_device" in pre
