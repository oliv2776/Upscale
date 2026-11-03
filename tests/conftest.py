from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as uscale  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe requis")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Instance isolée : racine de données, état SQLite et logs dans un dossier temporaire."""
    data = tmp_path / "data"
    (data / "videos").mkdir(parents=True)
    state = tmp_path / "state"
    monkeypatch.setattr(uscale, "DATA_ROOT", data.resolve())
    monkeypatch.setattr(uscale, "HOST_DATA", "/srv/nas/media")
    monkeypatch.setattr(uscale, "STATE_DIR", state)
    monkeypatch.setattr(uscale, "DB_PATH", state / "uscale.sqlite3")
    monkeypatch.setattr(uscale, "LOG_DIR", state / "logs")
    monkeypatch.setattr(uscale, "MIN_FREE_BYTES", 0)
    monkeypatch.setattr(uscale, "AUTH_PASSWORD", "")
    monkeypatch.setattr(uscale, "cancel_job_id", None)
    monkeypatch.setattr(uscale, "active_job_id", None)
    uscale.init_db()
    uscale.store_settings({
        **uscale.DEFAULT_SETTINGS,
        "source": str(data / "videos"),
        "destination": str(data / "out"),
    })
    return data


def make_video(path: Path, *, width=320, height=240, seconds=1, extra: list[str] | None = None,
               subtitles: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=10:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    if subtitles:
        srt = path.with_suffix(".srt")
        srt.write_text("1\n00:00:00,000 --> 00:00:00,800\nBonjour\n", encoding="utf-8")
        cmd += ["-i", str(srt), "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:s", "mov_text"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", *(extra or []), str(path)]
    subprocess.run(cmd, check=True)
    if subtitles:
        path.with_suffix(".srt").unlink()
    return path


FAKE_DOCKER = textwrap.dedent("""\
    #!{python}
    # Faux client Docker : imite Video2X en agrandissant la vidéo avec FFmpeg.
    import json, os, subprocess, sys, time
    args = sys.argv[1:]
    with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
        log.write(json.dumps(args) + "\\n")
    if args[:2] == ["image", "inspect"] or args[:1] in (["rm"], ["stop"], ["ps"], ["pull"], ["info"]):
        sys.exit(0)
    if args[:1] == ["run"]:
        image_index = args.index(os.environ["FAKE_DOCKER_IMAGE"])
        tool = args[image_index + 1:]
        if "--list-gpus" in tool:
            print("0. AMD Radeon RX 5500 XT (RADV NAVI14)")
            sys.exit(0)
        src = tool[tool.index("-i") + 1]
        dst = tool[tool.index("-o") + 1]
        scale = int(tool[tool.index("-s") + 1])
        delay = float(os.environ.get("FAKE_DOCKER_DELAY", "0"))
        for i in range(1, 6):
            sys.stderr.write(f"\\rProcessing frame {{i * 2}}/10 ({{i * 20}}.00%); time elapsed 00:00:0{{i}}")
            sys.stderr.flush()
            time.sleep(delay)
        sys.exit(subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-map", "0:v:0",
                                 "-vf", f"scale=iw*{{scale}}:ih*{{scale}}", "-c:v", "ffv1", dst]).returncode)
    sys.exit(1)
""")


@pytest.fixture()
def fake_docker(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "docker"
    script.write_text(FAKE_DOCKER.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "docker-calls.jsonl"
    log.touch()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_DOCKER_IMAGE", uscale.VIDEO2X_IMAGE)
    return log
