from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from upscale import config, db, gpu, runner, scanner, settings  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe requis")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Instance isolée : racine de données, état SQLite et journaux dans un dossier temporaire."""
    data = tmp_path / "data"
    (data / "videos").mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_ROOT", data.resolve())
    monkeypatch.setattr(config, "HOST_DATA", "/srv/nas/media")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "MIN_FREE_BYTES", 0)
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")
    monkeypatch.setattr(config, "RUNNER", "docker")
    monkeypatch.setattr(config, "VIDEO2X_GPU", "")
    monkeypatch.setattr(config, "SEGMENT_SECONDS", 180)
    monkeypatch.setattr(config, "PUID", None)
    monkeypatch.setattr(config, "PGID", None)
    monkeypatch.setattr(runner, "active_job_id", None)
    monkeypatch.setattr(runner, "cancel_job_id", None)
    # Les tests pilotent le worker eux-mêmes : pas de thread de fond qui volerait les tâches.
    from upscale import worker
    monkeypatch.setattr(worker, "ensure_worker", lambda: None)
    scanner.scan_state.clear()
    scanner.scan_state["running"] = False
    gpu.gpu_state.clear()
    gpu.gpu_state["status"] = "never"
    db.init_db()
    db.store_settings_payload({
        **settings.DEFAULT_SETTINGS,
        "source": str(data / "videos"),
        "destination": str(data / "out"),
        "preset": "ultrafast",
    })
    return data


def make_video(path: Path, *, width=320, height=240, seconds=1, rate=10, extra: list[str] | None = None,
               subtitles: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate={rate}:duration={seconds}",
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


FAKE_VIDEO2X = textwrap.dedent("""\
    #!{python}
    # Faux Docker / faux Video2X : Real-ESRGAN = agrandissement FFmpeg, RIFE = doublement des images.
    import json, os, subprocess, sys, time
    args = sys.argv[1:]
    with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
        log.write(json.dumps([os.path.basename(sys.argv[0]), *args]) + "\\n")
    if os.path.basename(sys.argv[0]) == "docker":
        if args[:2] == ["image", "inspect"] or args[:1] in (["rm"], ["stop"], ["ps"], ["pull"], ["info"]):
            sys.exit(0)
        if args[:1] != ["run"]:
            sys.exit(1)
        tool = args[args.index(os.environ["FAKE_DOCKER_IMAGE"]) + 1:]
    else:
        tool = args
    if "--list-devices" in tool:
        print("0. AMD Radeon RX 5500 XT (RADV NAVI14)")
        sys.exit(0)
    src = tool[tool.index("-i") + 1]
    dst = tool[tool.index("-o") + 1]
    delay = float(os.environ.get("FAKE_DOCKER_DELAY", "0"))
    for i in range(1, 6):
        sys.stderr.write(f"\\rProcessing frame {{i * 2}}/10 ({{i * 20}}.00%); time elapsed 00:00:0{{i}}")
        sys.stderr.flush()
        time.sleep(delay)
    if tool[tool.index("-p") + 1] == "rife":
        mul = tool[tool.index("--frame-rate-mul") + 1]
        vf = f"fps=fps=source_fps*{{mul}}"
    else:
        scale = int(tool[tool.index("-s") + 1])
        vf = f"scale=iw*{{scale}}:ih*{{scale}}"
    sys.exit(subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-map", "0:v:0",
                             "-vf", vf, "-c:v", "ffv1", dst]).returncode)
""")


@pytest.fixture()
def fake_docker(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("docker", "video2x"):
        script = bindir / name
        script.write_text(FAKE_VIDEO2X.format(python=sys.executable))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "docker-calls.jsonl"
    log.touch()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_DOCKER_IMAGE", config.VIDEO2X_IMAGE)
    monkeypatch.setattr(config, "VIDEO2X_BIN", str(bindir / "video2x"))
    return log


def calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


class Recorder(BaseHTTPRequestHandler):
    responses: dict[str, tuple[int, bytes]] = {}
    received: list[dict] = []

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.received.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
        status, payload = self.responses.get(self.path.split("?")[0], (200, b"{}"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _handle

    def log_message(self, *args):
        pass


@pytest.fixture()
def http_server():
    Recorder.responses = {}
    Recorder.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", Recorder
    server.shutdown()
