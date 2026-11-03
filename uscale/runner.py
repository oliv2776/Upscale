"""Exécution des processus (FFmpeg, Video2X) avec journal, progression, annulation et garde disque."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, db


class JobCancelled(Exception):
    pass


class JobError(Exception):
    pass


class JobDeferred(Exception):
    """La tâche s'interrompt proprement entre deux segments (pause ou hors plage horaire)."""


state_lock = threading.Lock()
active_job_id: str | None = None
cancel_job_id: str | None = None


def request_cancel(job_id: str) -> bool:
    global cancel_job_id
    with state_lock:
        if active_job_id == job_id:
            cancel_job_id = job_id
            return True
    return False


def is_cancel_requested(job_id: str) -> bool:
    with state_lock:
        return cancel_job_id == job_id


def clear_cancel(job_id: str) -> None:
    global cancel_job_id
    with state_lock:
        if cancel_job_id == job_id:
            cancel_job_id = None


def set_active(job_id: str | None) -> None:
    global active_job_id
    with state_lock:
        active_job_id = job_id


def get_active() -> str | None:
    with state_lock:
        return active_job_id


def human_size(value: float) -> str:
    return f"{value / 1024**3:.1f} Go"


# ---------------------------------------------------------------- progression

class Progress:
    """Progression globale d'une tâche (0..1) avec estimation du temps restant, écrite au plus toutes les 3 s."""

    def __init__(self, job_id: str, start: float = 0.0):
        self.job_id = job_id
        self.start_value = start
        self.value = start
        self.started = time.monotonic()
        self.last_write = 0.0

    def update(self, value: float, force: bool = False) -> None:
        value = max(self.value, min(value, 1.0))
        self.value = value
        current = time.monotonic()
        if not force and current - self.last_write < 3:
            return
        self.last_write = current
        done = value - self.start_value
        eta = int((current - self.started) * (1 - value) / done) if done > 0.002 else None
        db.set_job(self.job_id, progress=round(value, 4), eta_seconds=eta)

    def span(self, base: float, width: float) -> Callable[[float], None]:
        return lambda local: self.update(base + width * local)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FRAME_RE = re.compile(r"frames?\D{0,20}?(\d+)\s*/\s*(\d+)", re.IGNORECASE)
PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
OUT_TIME_RE = re.compile(r"^out_time_(?:us|ms)=(\d+)$")
FFMPEG_PROGRESS_KEYS = ("frame=", "fps=", "stream_", "bitrate=", "total_size=", "out_time",
                        "dup_frames=", "drop_frames=", "speed=", "progress=")


def parse_video2x_progress(line: str) -> float | None:
    clean = ANSI_RE.sub("", line)
    match = FRAME_RE.search(clean)
    if match and int(match.group(2)) > 0:
        return min(int(match.group(1)) / int(match.group(2)), 1.0)
    match = PERCENT_RE.search(clean)
    if match and "frame" in clean.lower():
        return min(float(match.group(1)) / 100, 1.0)
    return None


def ffmpeg_progress_parser(duration: float) -> Callable[[str], float | None]:
    def parse(line: str) -> float | None:
        match = OUT_TIME_RE.match(line.strip())
        if match and duration > 0:
            return min(int(match.group(1)) / 1_000_000 / duration, 1.0)
        return None
    return parse


# ---------------------------------------------------------------- processus

def docker_remove(name: str) -> None:
    try:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass


def stop_process(proc: subprocess.Popen, container: str | None) -> None:
    if container:
        try:
            subprocess.run(["docker", "stop", "-t", "10", container], capture_output=True, timeout=40)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    if container:
        docker_remove(container)


def run_process(job_id: str, command: list[str], *, container: str | None = None,
                parse_progress: Callable[[str], float | None] | None = None,
                on_progress: Callable[[float], None] | None = None,
                disk_dir: Path | None = None) -> int:
    """Lance une commande, journalise sa sortie, suit la progression, l'annulation et l'espace disque."""
    db.log_line(job_id, "$ " + " ".join(command))
    last_log = [0.0]

    def handle(line: str) -> None:
        if not line.strip():
            return
        value = parse_progress(line) if parse_progress else None
        if value is None:
            if not line.startswith(FFMPEG_PROGRESS_KEYS):
                db.log_line(job_id, ANSI_RE.sub("", line))
            return
        if time.monotonic() - last_log[0] >= 60:
            db.log_line(job_id, f"[{db.now()}] progression de l'étape {value * 100:.1f} %")
            last_log[0] = time.monotonic()
        if on_progress:
            on_progress(value)

    def reader(stream: Any) -> None:
        buffer = b""
        for chunk in iter(lambda: stream.read1(65536), b""):
            buffer += chunk
            parts = re.split(rb"[\r\n]", buffer)
            buffer = parts.pop()
            for part in parts:
                handle(part.decode("utf-8", errors="replace"))
        if buffer:
            handle(buffer.decode("utf-8", errors="replace"))

    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    thread = threading.Thread(target=reader, args=(proc.stdout,), daemon=True)
    thread.start()
    last_disk_check = 0.0
    try:
        while proc.poll() is None:
            if is_cancel_requested(job_id):
                raise JobCancelled("Traitement annulé")
            if disk_dir and time.monotonic() - last_disk_check >= 15:
                last_disk_check = time.monotonic()
                free = shutil.disk_usage(disk_dir).free
                if free < config.MIN_FREE_BYTES:
                    raise JobError(f"Espace disque presque plein ({human_size(free)} libres) : traitement arrêté")
            time.sleep(0.3)
    except BaseException:
        stop_process(proc, container)
        raise
    finally:
        thread.join(timeout=10)
    return proc.returncode


def check(code: int, message: str) -> None:
    if code != 0:
        raise JobError(f"{message} (code {code}); consulter le journal")


# ---------------------------------------------------------------- Video2X

def container_name(job_id: str) -> str:
    return f"uscale-{job_id}"


def video2x_command(job_id: str, args: list[str]) -> tuple[list[str], str | None]:
    """Commande Video2X selon le mode d'exécution, et nom du conteneur éventuel."""
    if config.VIDEO2X_GPU:
        args = [*args, "-g", config.VIDEO2X_GPU]
    if config.RUNNER == "local":
        return [config.VIDEO2X_BIN, *args], None
    name = container_name(job_id)
    return [
        "docker", "run", "--rm", "--name", name,
        "--label", config.CONTAINER_LABEL, "--label", f"uscale.job={job_id}",
        "--device", "/dev/dri:/dev/dri",
        "--group-add", config.VIDEO_GID, "--group-add", config.RENDER_GID,
        # Le daemon Docker interprète la source du montage comme un chemin de l'hôte.
        "-v", f"{config.HOST_DATA}:{config.DATA_ROOT}",
        config.VIDEO2X_IMAGE, *args,
    ], name


def upscale_args(source: Path, output: Path, model: str, scale: int) -> list[str]:
    # Intermédiaire sans perte : l'unique encodage avec perte est fait ensuite par FFmpeg.
    return ["-i", str(source), "-o", str(output), "-p", "realesrgan", "-s", str(scale),
            "--realesrgan-model", model, "-c", "ffv1"]


def interpolate_args(source: Path, output: Path, multiplier: int) -> list[str]:
    return ["-i", str(source), "-o", str(output), "-p", "rife", "--frame-rate-mul", str(multiplier), "-c", "ffv1"]


def run_video2x(job_id: str, args: list[str], *, on_progress: Callable[[float], None] | None = None,
                disk_dir: Path | None = None) -> int:
    command, container = video2x_command(job_id, args)
    if container:
        docker_remove(container)
    return run_process(job_id, command, container=container, parse_progress=parse_video2x_progress,
                       on_progress=on_progress, disk_dir=disk_dir)


def ensure_image(job_id: str) -> None:
    if config.RUNNER == "local":
        return
    inspect = subprocess.run(["docker", "image", "inspect", config.VIDEO2X_IMAGE], capture_output=True, timeout=30)
    if inspect.returncode == 0:
        return
    db.set_job(job_id, stage="Téléchargement de l'image Video2X")
    check(run_process(job_id, ["docker", "pull", config.VIDEO2X_IMAGE]), f"Impossible de télécharger {config.VIDEO2X_IMAGE}")


def cleanup_orphan_containers() -> None:
    """Un redémarrage d'Uscale n'arrête pas les conteneurs Video2X frères : on les supprime."""
    if config.RUNNER == "local" or not shutil.which("docker"):
        return
    try:
        listing = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={config.CONTAINER_LABEL}"],
                                 capture_output=True, text=True, timeout=30)
        for container_id in listing.stdout.split():
            docker_remove(container_id)
    except (OSError, subprocess.TimeoutExpired):
        pass


def chown(path: Path) -> None:
    """Donne le fichier à l'utilisateur du NAS (PUID/PGID) plutôt qu'à root."""
    if config.PUID is None and config.PGID is None:
        return
    try:
        os.chown(path, config.PUID if config.PUID is not None else -1, config.PGID if config.PGID is not None else -1)
    except OSError:
        pass
