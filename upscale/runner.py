"""Exécution des processus (FFmpeg, Video2X) avec journal, progression, annulation et garde disque."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, db, models


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

class Throughput:
    """Vitesse de traitement mesurée (images par seconde) et temps restant qui en découle.

    Seul le temps passé à traiter des segments (IA puis encodage) est compté : ni la découpe initiale, ni les
    pauses, ni les segments déjà faits lors d'une exécution précédente ne faussent l'estimation.
    """

    MIN_SECONDS = 20.0  # en dessous, la mesure est trop bruitée pour annoncer une durée

    def __init__(self, total_frames: float, clock: Callable[[], float] = time.monotonic):
        self.total = max(float(total_frames), 1.0)
        self.clock = clock
        self.skipped = 0.0   # images déjà traitées avant cette exécution
        self.done = 0.0      # images traitées pendant cette exécution (segments terminés)
        self.active = 0.0    # secondes passées sur les segments terminés
        self.current = 0.0   # images du segment en cours
        self.started: float | None = None

    def skip(self, frames: float) -> None:
        self.skipped += frames

    def begin(self) -> None:
        self.started = self.clock()
        self.current = 0.0

    def advance(self, frames: float) -> None:
        self.current = max(self.current, frames)

    def end(self, frames: float) -> None:
        if self.started is not None:
            self.active += self.clock() - self.started
        self.started = None
        self.done += frames
        self.current = 0.0

    def estimate(self) -> tuple[float | None, int | None]:
        """(images par seconde, secondes restantes) ; (None, None) tant que la mesure est insuffisante."""
        elapsed = self.active + (self.clock() - self.started if self.started is not None else 0.0)
        frames = self.done + self.current
        if elapsed < self.MIN_SECONDS or frames <= 0:
            return None, None
        fps = frames / elapsed
        remaining = max(self.total - self.skipped - frames, 0.0)
        return round(fps, 2), int(remaining / fps)


class Progress:
    """Progression globale d'une tâche (0..1) avec estimation du temps restant, écrite au plus toutes les 3 s."""

    def __init__(self, job_id: str, start: float = 0.0, throughput: Throughput | None = None):
        self.job_id = job_id
        self.throughput = throughput
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
        if self.throughput is not None:
            # Tâche d'agrandissement : temps restant d'après la vitesse réellement mesurée de l'IA.
            fps, eta = self.throughput.estimate()
            db.set_job(self.job_id, progress=round(value, 4), eta_seconds=eta, fps=fps)
            return
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
        if os.name == "posix":
            try:
                proc.send_signal(signal.SIGCONT)  # un processus suspendu ignorerait SIGTERM
            except OSError:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    if container:
        docker_remove(container)


class RunningProcess:
    """Processus lancé en arrière-plan : sortie journalisée, progression suivie, arrêt propre."""

    def __init__(self, job_id: str, command: list[str], *, container: str | None = None,
                 parse_progress: Callable[[str], float | None] | None = None,
                 on_progress: Callable[[float], None] | None = None, cwd: str | None = None):
        self.job_id, self.container = job_id, container
        self.parse_progress, self.on_progress = parse_progress, on_progress
        self.last_log = 0.0
        self._paused = False
        db.log_line(job_id, "$ " + " ".join(command))
        self.proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, cwd=cwd)
        self.thread = threading.Thread(target=self._reader, args=(self.proc.stdout,), daemon=True)
        self.thread.start()

    def _handle(self, line: str) -> None:
        if not line.strip():
            return
        value = self.parse_progress(line) if self.parse_progress else None
        if value is None:
            if not line.startswith(FFMPEG_PROGRESS_KEYS):
                db.log_line(self.job_id, ANSI_RE.sub("", line))
            return
        if time.monotonic() - self.last_log >= 60:
            db.log_line(self.job_id, f"[{db.now()}] progression de l'étape {value * 100:.1f} %")
            self.last_log = time.monotonic()
        if self.on_progress:
            self.on_progress(value)

    def _reader(self, stream: Any) -> None:
        buffer = b""
        for chunk in iter(lambda: stream.read1(65536), b""):
            buffer += chunk
            parts = re.split(rb"[\r\n]", buffer)
            buffer = parts.pop()
            for part in parts:
                self._handle(part.decode("utf-8", errors="replace"))
        if buffer:
            self._handle(buffer.decode("utf-8", errors="replace"))

    def running(self) -> bool:
        return self.proc.poll() is None

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Suspend le processus (SIGSTOP) : utilisé pour borner l'espace disque de la découpe."""
        if self._paused or not self.running() or os.name != "posix":
            return
        try:
            os.kill(self.proc.pid, signal.SIGSTOP)
            self._paused = True
        except (OSError, ProcessLookupError):
            pass

    def resume(self) -> None:
        """Reprend un processus suspendu (SIGCONT)."""
        if not self._paused:
            return
        try:
            os.kill(self.proc.pid, signal.SIGCONT)
        except (OSError, ProcessLookupError):
            pass
        self._paused = False

    def stop(self) -> None:
        self.resume()
        stop_process(self.proc, self.container)
        self.finish()

    def finish(self) -> None:
        self.thread.join(timeout=10)


def check_disk(disk_dir: Path) -> None:
    free = shutil.disk_usage(disk_dir).free
    if free < config.MIN_FREE_BYTES:
        raise JobError(f"Espace disque presque plein ({human_size(free)} libres) : traitement arrêté")


def run_process(job_id: str, command: list[str], *, container: str | None = None,
                parse_progress: Callable[[str], float | None] | None = None,
                on_progress: Callable[[float], None] | None = None,
                disk_dir: Path | None = None, cwd: str | None = None,
                poll: Callable[[], None] | None = None) -> int:
    """Lance une commande et attend sa fin, en suivant progression, annulation et espace disque."""
    process = RunningProcess(job_id, command, container=container, parse_progress=parse_progress,
                             on_progress=on_progress, cwd=cwd)
    last_disk_check = 0.0
    try:
        while process.running():
            if is_cancel_requested(job_id):
                raise JobCancelled("Traitement annulé")
            if poll:
                poll()
            if disk_dir and time.monotonic() - last_disk_check >= 15:
                last_disk_check = time.monotonic()
                check_disk(disk_dir)
            time.sleep(0.3)
    except BaseException:
        process.stop()
        raise
    process.finish()
    return process.proc.returncode


def check(code: int, message: str) -> None:
    if code != 0:
        raise JobError(f"{message} (code {code}); consulter le journal")


# ---------------------------------------------------------------- Video2X

def container_name(job_id: str) -> str:
    return f"upscale-{job_id}"


def video2x_command(job_id: str, args: list[str]) -> tuple[list[str], str | None]:
    """Commande Video2X selon le mode d'exécution, et nom du conteneur éventuel."""
    if config.VIDEO2X_GPU:
        args = [*args, "-d", config.VIDEO2X_GPU]  # index du périphérique Vulkan
    if config.RUNNER == "local":
        return [config.VIDEO2X_BIN, *args], None
    name = container_name(job_id)
    return [
        "docker", "run", "--rm", "--name", name,
        "--label", config.CONTAINER_LABEL, "--label", f"upscale.job={job_id}",
        "--device", "/dev/dri:/dev/dri",
        "--group-add", config.VIDEO_GID, "--group-add", config.RENDER_GID,
        # Le daemon Docker interprète la source du montage comme un chemin de l'hôte :
        # on redonne à Video2X exactement les mêmes disques que ceux d'Upscale.
        *[arg for host, inside in library_mounts() for arg in ("-v", f"{host}:{inside}")],
        config.VIDEO2X_IMAGE, *args,
    ], name


_mounts_cache: list[tuple[str, str]] | None = None


def library_mounts() -> list[tuple[str, str]]:
    """Couples (chemin hôte, chemin dans le conteneur) des disques visibles par Upscale.

    Lus dans la configuration du conteneur Upscale lui-même (docker inspect), ce qui suit automatiquement
    les disques ajoutés avec bibliotheques.sh. Repli : UPSCALE_HOST_DATA monté sur /data.
    """
    global _mounts_cache
    if _mounts_cache is not None:
        return _mounts_cache
    found: list[tuple[str, str]] = []
    for name in dict.fromkeys([socket.gethostname(), "upscale"]):
        try:
            result = subprocess.run(["docker", "inspect", "--format", "{{json .Mounts}}", name],
                                    capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            break
        if result.returncode != 0:
            continue
        try:
            mounts = json.loads(result.stdout or "[]") or []
        except ValueError:
            continue
        for mount in mounts:
            inside = Path(str(mount.get("Destination", "")))
            if mount.get("Type") != "bind":
                continue
            if inside == config.DATA_ROOT or config.DISKS_ROOT in inside.parents:
                found.append((str(mount["Source"]), str(inside)))
        if found:
            break
    if not any(Path(inside) == config.DATA_ROOT for _, inside in found):
        found.insert(0, (config.HOST_DATA, str(config.DATA_ROOT)))
    _mounts_cache = sorted(found, key=lambda pair: pair[1])
    return _mounts_cache


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
                       on_progress=on_progress, disk_dir=disk_dir, cwd=video2x_cwd())


def video2x_cwd() -> str | None:
    if config.RUNNER == "local" and config.VIDEO2X_WORKDIR and Path(config.VIDEO2X_WORKDIR).is_dir():
        return config.VIDEO2X_WORKDIR
    return None


def ensure_image(job_id: str) -> None:
    if config.RUNNER == "local":
        return
    inspect = subprocess.run(["docker", "image", "inspect", config.VIDEO2X_IMAGE], capture_output=True, timeout=30)
    if inspect.returncode == 0:
        return
    db.set_job(job_id, stage="Téléchargement de l'image Video2X")
    check(run_process(job_id, ["docker", "pull", config.VIDEO2X_IMAGE]), f"Impossible de télécharger {config.VIDEO2X_IMAGE}")


def cleanup_orphan_containers() -> None:
    """Un redémarrage d'Upscale n'arrête pas les conteneurs Video2X frères : on les supprime."""
    if config.RUNNER == "local" or not shutil.which("docker"):
        return
    try:
        for label in (config.CONTAINER_LABEL, config.LEGACY_CONTAINER_LABEL):
            listing = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={label}"],
                                     capture_output=True, text=True, timeout=30)
            for container_id in listing.stdout.split():
                docker_remove(container_id)
    except (OSError, subprocess.TimeoutExpired):
        pass


Owner = tuple[int, int]


def default_owner(source: Path, *folders: Path) -> Owner | None:
    """Propriétaire des fichiers produits.

    PUID/PGID s'ils sont définis ; sinon le même propriétaire que la vidéo d'origine, puis celui du
    dossier de destination ou source. Root n'est jamais choisi par défaut : le conteneur tourne en root,
    mais les fichiers doivent appartenir à l'utilisateur qui gère la bibliothèque.
    """
    if config.PUID is not None or config.PGID is not None:
        return (config.PUID if config.PUID is not None else -1, config.PGID if config.PGID is not None else -1)
    candidates = [source]
    for folder in folders:
        candidates += [folder, *[p for p in folder.parents if config.root_of(p) is not None]]
    for candidate in candidates:
        try:
            info = candidate.stat()
        except OSError:
            continue
        if info.st_uid != 0:
            return info.st_uid, info.st_gid
    return None


def chown(path: Path, owner: Owner | None) -> None:
    if owner is None:
        return
    try:
        os.chown(path, owner[0], owner[1])
    except OSError:
        pass


# ---------------------------------------------------------------- modèles personnalisés (moteur ncnn)

def scale_flag() -> str:
    """Facteur du modèle : -z pour upscayl-bin (-s y redimensionne la sortie), -s pour realesrgan-ncnn-vulkan."""
    return "-s" if "realesrgan-ncnn-vulkan" in Path(config.NCNN_BIN).name else "-z"


def ncnn_command(frames_in: Path, frames_out: Path, meta: dict[str, Any]) -> list[str]:
    command = [config.NCNN_BIN, "-i", str(frames_in), "-o", str(frames_out), "-m", meta["folder"],
               "-n", models.NCNN_NAME, scale_flag(), str(meta["scale"]), "-f", "png"]
    if int(meta.get("tile") or 0):
        command += ["-t", str(meta["tile"])]
    if config.VIDEO2X_GPU:
        command += ["-g", config.VIDEO2X_GPU]
    return command


def run_custom_upscale(job_id: str, source: Path, output: Path, meta: dict[str, Any], fps: float, *,
                       on_progress: Callable[[float], None] | None = None, disk_dir: Path | None = None,
                       matrix: str | None = None) -> None:
    """Agrandit une vidéo courte (un segment) avec un modèle personnalisé, image par image.

    Images extraites en PNG -> moteur ncnn (upscayl-bin) -> nouveau segment FFV1 intra sans perte.
    matrix : matrice de couleur de la source, appliquée à l'aller (YUV -> RVB) comme au retour, pour que
    le modèle voie les vraies couleurs et que le segment produit reste dans l'espace de la source.
    """
    to_rgb = ["-vf", f"scale=in_color_matrix={matrix},format=rgb24"] if matrix else ["-pix_fmt", "rgb24"]
    to_yuv = ["-vf", f"scale=out_color_matrix={matrix},format=yuv420p"] if matrix else ["-pix_fmt", "yuv420p"]
    work = output.with_name(output.stem + ".frames")
    shutil.rmtree(work, ignore_errors=True)
    frames_in, frames_out = work / "in", work / "out"
    frames_in.mkdir(parents=True)
    frames_out.mkdir()
    try:
        check(run_process(job_id, ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(source),
                                   "-map", "0:v:0", "-start_number", "0", *to_rgb,
                                   "-compression_level", "1", str(frames_in / "%08d.png")], disk_dir=disk_dir),
              "Extraction des images impossible")
        total = sum(1 for _ in frames_in.iterdir())
        if not total:
            raise JobError("Aucune image extraite du segment")
        last = [0.0]

        def poll() -> None:
            if on_progress and time.monotonic() - last[0] >= 2:
                last[0] = time.monotonic()
                on_progress(min(sum(1 for _ in frames_out.iterdir()) / total, 1.0))

        check(run_process(job_id, ncnn_command(frames_in, frames_out, meta), disk_dir=disk_dir, poll=poll),
              f"Le modèle personnalisé « {meta['label']} » a échoué")
        done = sum(1 for _ in frames_out.iterdir())
        if done != total:
            raise JobError(f"Le modèle personnalisé n'a produit que {done} image(s) sur {total} "
                           "(modèle incompatible avec le moteur, ou facteur d'échelle incorrect ?)")
        shutil.rmtree(frames_in, ignore_errors=True)
        check(run_process(job_id, ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error",
                                   "-framerate", f"{fps:.6f}", "-start_number", "0", "-i", str(frames_out / "%08d.png"),
                                   *to_yuv, "-c:v", "ffv1", "-g", "1", str(output)], disk_dir=disk_dir),
              "Reconstitution de la vidéo impossible")
    finally:
        shutil.rmtree(work, ignore_errors=True)
