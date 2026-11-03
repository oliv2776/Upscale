"""Installation de modèles depuis OpenModelDB ou un fichier (.pth, .safetensors, .onnx, .zip, ncnn).

Étapes d'une installation (une à la fois, en arrière-plan) :
1. téléchargement (lien OpenModelDB ou lien direct) avec contrôle de taille et d'empreinte SHA-256 ;
2. conversion en ncnn si besoin, par convert_model.py dans un environnement séparé (torch CPU + spandrel + pnnx)
   installé automatiquement à la première conversion ;
3. vérification avec le vrai moteur Vulkan (upscayl-bin) sur une image de test : il doit charger le modèle, produire la
   bonne taille et, quand le modèle PyTorch d'origine est disponible, une image identique à sa référence ;
4. enregistrement comme modèle personnalisé (models.py), avec sa provenance et sa licence.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, models

log = logging.getLogger("upscale.converter")

# Environnement du convertisseur. torch/torchvision viennent de l'index « CPU » de PyTorch (sans CUDA,
# environ 5 fois plus léger) ; le reste de PyPI. Versions figées : testées ensemble en CI.
TORCH_PACKAGES = ["torch==2.15.0", "torchvision==0.29.2"]
CONVERTER_PACKAGES = ["spandrel==0.4.2", "pnnx==20261020", "onnx==1.23.2", "numpy==2.5.4"]
CONVERTER_FREE_BYTES = 3 * 1024**3
CONVERT_SCRIPT = Path(__file__).with_name("convert_model.py")
MODEL_EXTENSIONS = {".pth", ".pt", ".ckpt", ".safetensors", ".onnx"}
SUGGESTIONS = [
    {"id": "2x-LiveActionV1-SPAN", "label": "2xLiveActionV1_SPAN",
     "why": "Vidéo filmée compressée (H.264, MPEG-4, VP9) : retire blocs, flou et halos sans lisser la peau. "
            "Ne débruite pas : garde le débruitage « Moyen ». Licence non commerciale."},
]
TEST_SIZE = (96, 64)
MIN_PSNR = 28.0  # en dessous, la conversion a changé le résultat du modèle
USER_AGENT = f"Upscale/{config.VERSION} (+https://github.com/oliv2776/Upscale)"
OPENMODELDB_RE = re.compile(r"openmodeldb\.info/models/([A-Za-z0-9][A-Za-z0-9._-]{0,120})")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
KEEP_FINISHED = 8

_lock = threading.Lock()
_queue: queue.Queue[str] = queue.Queue()
_thread: threading.Thread | None = None
INSTALLS: dict[str, dict[str, Any]] = {}


class InstallError(Exception):
    pass


# ---------------------------------------------------------------- suivi des installations

def public(task: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in task.items() if not k.startswith("_")}


def list_installs() -> list[dict[str, Any]]:
    with _lock:
        return [public(t) for t in sorted(INSTALLS.values(), key=lambda t: t["created"], reverse=True)]


def get_install(install_id: str) -> dict[str, Any] | None:
    with _lock:
        task = INSTALLS.get(install_id)
        return public(task) if task else None


def _update(task: dict[str, Any], **values: Any) -> None:
    with _lock:
        task.update(values)


def _log(task: dict[str, Any], line: str) -> None:
    line = line.rstrip()
    if not line:
        return
    log.info("[installation %s] %s", task["id"], line)
    with _lock:
        task["log"] = (task["log"] + [line[:400]])[-60:]


def _step(task: dict[str, Any], step: str, progress: float | None = None) -> None:
    _update(task, step=step, progress=progress)
    _log(task, f"== {step}")


def _prune() -> None:
    with _lock:
        finished = sorted((t for t in INSTALLS.values() if t["state"] in {"done", "error"}),
                          key=lambda t: t["created"], reverse=True)
        for task in finished[KEEP_FINISHED:]:
            INSTALLS.pop(task["id"], None)


def submit(kind: str, source: str, *, label: str = "", scale: Any = None, tile: Any = 0,
           upload: Path | None = None) -> dict[str, Any]:
    """Met une installation en file. kind : « openmodeldb », « url » ou « file » (fichier envoyé)."""
    try:
        tile = int(tile or 0)
    except (TypeError, ValueError):
        raise ValueError("Taille de tuile invalide") from None
    if tile not in models.TILES:
        raise ValueError("Taille de tuile invalide")
    if scale not in (None, ""):
        try:
            scale = int(scale)
        except (TypeError, ValueError):
            raise ValueError("Facteur invalide") from None
        if scale not in models.SCALES:
            raise ValueError("Facteur d'échelle : 2, 3 ou 4")
    else:
        scale = None
    task = {"id": uuid.uuid4().hex[:12], "kind": kind, "source": source, "label": label.strip()[:models.MAX_LABEL],
            "scale": scale, "tile": tile, "state": "queued", "step": "En attente", "progress": None,
            "message": "", "model_id": None, "created": time.time(), "finished": None, "log": [],
            "_upload": str(upload) if upload else None}
    _prune()
    with _lock:
        INSTALLS[task["id"]] = task
    _queue.put(task["id"])
    start_worker()
    return public(task)


def start_worker() -> None:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _thread = threading.Thread(target=_worker, name="upscale-models", daemon=True)
        _thread.start()


def _worker() -> None:
    while True:
        install_id = _queue.get()
        try:
            process(install_id)
        except Exception:  # noqa: BLE001 - le fil ne doit jamais s'arrêter
            log.exception("Installation %s", install_id)


def process_pending() -> None:
    """Traite les installations en file dans le fil courant (tests)."""
    while True:
        try:
            install_id = _queue.get_nowait()
        except queue.Empty:
            return
        process(install_id)


def staging_root() -> Path:
    return models.models_dir()  # même disque que la destination : rename atomique, chemin contenant « models »


def cleanup_staging() -> None:
    """Supprime les dossiers de travail laissés par un redémarrage pendant une installation."""
    root = staging_root()
    if not root.is_dir():
        return
    for folder in root.iterdir():
        if folder.is_dir() and (folder.name.startswith((".installation-", ".envoi-")) or folder.name.endswith(".import")):
            shutil.rmtree(folder, ignore_errors=True)


def process(install_id: str) -> None:
    with _lock:
        task = INSTALLS.get(install_id)
    if not task or task["state"] != "queued":
        return
    work = staging_root() / f".installation-{install_id}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    _update(task, state="running")
    try:
        meta = install(task, work)
        _update(task, state="done", step="Installé", progress=1.0, model_id=meta["id"], label=meta["label"],
                message=f"Modèle « {meta['label']} » installé et vérifié : compare-le dans l'Aperçu.")
    except InstallError as exc:
        _update(task, state="error", step="Échec", message=str(exc))
        _log(task, f"Erreur : {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Installation %s", install_id)
        _update(task, state="error", step="Échec", message=f"Erreur inattendue : {exc}")
    finally:
        _update(task, finished=time.time())
        shutil.rmtree(work, ignore_errors=True)
        upload = task.get("_upload")
        if upload:
            shutil.rmtree(Path(upload).parent, ignore_errors=True)


# ---------------------------------------------------------------- installation

def install(task: dict[str, Any], work: Path) -> dict[str, Any]:
    info: dict[str, Any] = {}
    candidates: list[tuple[dict[str, Any], str]] = []  # (ressource, url)
    if task["kind"] == "openmodeldb":
        _step(task, "Lecture de la fiche OpenModelDB")
        info = openmodeldb_info(task["source"])
        _log(task, f"{info['name']} — {info.get('author') or 'auteur inconnu'}, ×{info.get('scale')}, "
                   f"{info.get('architecture')}, licence {info.get('license') or 'non précisée'}")
        if info.get("scale") not in models.SCALES:
            raise InstallError(f"Modèle ×{info.get('scale')} : le moteur ne prend en charge que ×2, ×3 et ×4")
        if info.get("inputChannels", 3) != 3 or info.get("outputChannels", 3) != 3:
            raise InstallError("Modèle non couleur (canaux ≠ 3) : inutilisable pour la vidéo")
        for resource in ordered_resources(info.get("resources") or []):
            candidates += [(resource, url) for url in resource.get("urls") or []]
        if not candidates:
            raise InstallError("Aucun fichier téléchargeable dans la fiche de ce modèle")
    elif task["kind"] == "url":
        candidates = [({}, task["source"])]

    if task["kind"] == "file":
        downloaded = Path(task["_upload"])
        resource: dict[str, Any] = {}
    else:
        downloaded, resource = download_first(task, candidates, work / "telechargement")

    _step(task, "Préparation des fichiers")
    model_file, ncnn_pair = unpack(downloaded, work / "extrait")
    scale = info.get("scale") or task["scale"]
    reference = None
    ref_size = None
    if ncnn_pair:
        param, binary = ncnn_pair
        details: dict[str, Any] = {"architecture": info.get("architecture") or "ncnn"}
    else:
        assert model_file is not None
        if model_file.suffix.lower() == ".onnx" and not scale:
            raise InstallError("Indique le facteur du modèle ONNX (×2, ×3 ou ×4) : il est écrit dans son nom")
        python = ensure_converter(task)
        _step(task, "Conversion au format ncnn")
        test_raw = make_test_image(work / "test")[1]
        out = work / "ncnn"
        details = run_converter(task, python, model_file, out, test_raw)
        param, binary = out / "model.param", out / "model.bin"
        if details.get("scale"):
            if scale and scale != details["scale"]:
                _log(task, f"Facteur réel du modèle : ×{details['scale']} (×{scale} annoncé)")
            scale = details["scale"]
        if details.get("reference_size"):
            reference, ref_size = out / "reference.rgb", tuple(details["reference_size"])
    if not scale:
        raise InstallError("Facteur du modèle inconnu : choisis ×2, ×3 ou ×4 dans le formulaire")

    psnr = verify(task, param, binary, scale, task["tile"], work / "verification", reference, ref_size)
    label = task["label"] or info.get("name") or Path(task["source"]).stem or "Modèle"
    extra = {"source": {"kind": task["kind"], "ref": task["source"] if task["kind"] != "file" else Path(task["source"]).name,
                        "file": resource.get("type") or (model_file.suffix.lstrip(".") if model_file else "ncnn")},
             "architecture": details.get("architecture"), "license": info.get("license"), "author": info.get("author"),
             "page": f"https://openmodeldb.info/models/{info['id']}" if info.get("id") else None,
             "verified": {"psnr": psnr, "engine": Path(config.NCNN_BIN).exists()}}
    try:
        meta = models.create_from_files(label[:models.MAX_LABEL], scale, task["tile"], param, binary, extra)
    except ValueError as exc:
        raise InstallError(str(exc)) from None
    _log(task, f"Enregistré sous {meta['id']}")
    return meta


def openmodeldb_id(text: str) -> str:
    text = text.strip()
    match = OPENMODELDB_RE.search(text)
    model_id = match.group(1) if match else text
    model_id = model_id.split("?")[0].split("#")[0].strip("/")
    if not MODEL_ID_RE.match(model_id):
        raise InstallError("Lien OpenModelDB invalide : colle l'adresse de la page du modèle "
                           "(https://openmodeldb.info/models/...)")
    return model_id


def openmodeldb_info(text: str) -> dict[str, Any]:
    model_id = openmodeldb_id(text)
    url = f"{config.OPENMODELDB_DATA.rstrip('/')}/{urllib.parse.quote(model_id)}.json"
    try:
        with urllib.request.urlopen(request(url), timeout=30) as response:
            info = json.loads(response.read(2_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise InstallError(f"Modèle « {model_id} » introuvable sur OpenModelDB") from None
        raise InstallError(f"OpenModelDB a répondu {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise InstallError(f"OpenModelDB injoignable : {getattr(exc, 'reason', exc)}") from None
    except ValueError:
        raise InstallError("Fiche OpenModelDB illisible") from None
    if not isinstance(info, dict):
        raise InstallError("Fiche OpenModelDB illisible")
    info["id"] = model_id
    return info


def ordered_resources(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ncnn déjà prêt (pas de conversion), puis PyTorch (conversion vérifiée), puis ONNX."""
    rank = {"ncnn": 0, "pytorch": 1, "onnx": 2}
    usable = [r for r in resources if isinstance(r, dict) and r.get("platform") in rank]
    return sorted(usable, key=lambda r: rank[r["platform"]])


def request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def direct_url(url: str) -> str | None:
    """Adresse de téléchargement direct, ou None si l'hébergeur n'est pas pris en charge."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    if host.endswith("drive.google.com"):
        if "/folders/" in parsed.path:
            return None
        match = re.search(r"/file/d/([A-Za-z0-9_-]+)", parsed.path)
        file_id = match.group(1) if match else urllib.parse.parse_qs(parsed.query).get("id", [None])[0]
        return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t" if file_id else None
    if host in {"mega.nz", "mega.co.nz"}:
        return None
    if host == "github.com" and "/blob/" in parsed.path:
        return url.replace("/blob/", "/raw/", 1)
    if host.endswith("huggingface.co") and "/blob/" in parsed.path:
        return url.replace("/blob/", "/resolve/", 1)
    return url


def download_first(task: dict[str, Any], candidates: list[tuple[dict[str, Any], str]],
                   folder: Path) -> tuple[Path, dict[str, Any]]:
    folder.mkdir(parents=True, exist_ok=True)
    errors = []
    for resource, url in candidates:
        target_url = direct_url(url)
        if not target_url:
            errors.append(f"{url} : hébergeur non pris en charge (dossier Google Drive, Mega...)")
            _log(task, errors[-1])
            continue
        _step(task, "Téléchargement", 0.0)
        _log(task, target_url)
        try:
            path = download(task, target_url, folder, resource.get("size"), resource.get("sha256"))
            return path, resource
        except InstallError as exc:
            errors.append(f"{url} : {exc}")
            _log(task, errors[-1])
    raise InstallError("Téléchargement impossible. " + " | ".join(errors[-3:]))


def download(task: dict[str, Any], url: str, folder: Path, size: Any = None, sha256: Any = None) -> Path:
    try:
        response = urllib.request.urlopen(request(url), timeout=60)
    except urllib.error.HTTPError as exc:
        raise InstallError(f"erreur HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise InstallError(f"serveur injoignable ({getattr(exc, 'reason', exc)})") from None
    with response:
        kind = response.headers.get("Content-Type", "")
        if kind.startswith("text/html"):
            raise InstallError("le lien mène à une page web, pas à un fichier")
        total = int(response.headers.get("Content-Length") or size or 0)
        if total > config.MODEL_MAX_BYTES:
            raise InstallError("fichier trop volumineux")
        name = filename_from(response, url)
        target = folder / name
        digest = hashlib.sha256()
        received = 0
        with target.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > config.MODEL_MAX_BYTES:
                    raise InstallError("fichier trop volumineux")
                digest.update(chunk)
                out.write(chunk)
                if total:
                    _update(task, progress=min(received / total, 1.0))
    if size and received != int(size):
        raise InstallError(f"taille inattendue ({received} octets au lieu de {size})")
    if sha256 and digest.hexdigest().lower() != str(sha256).lower():
        raise InstallError("empreinte SHA-256 différente de celle d'OpenModelDB (fichier modifié ou corrompu)")
    _log(task, f"{name} : {received / 1024**2:.1f} Mo" + (" , empreinte vérifiée" if sha256 else ""))
    return target


def filename_from(response: Any, url: str) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition)
    raw = urllib.parse.unquote(match.group(1)) if match else urllib.parse.unquote(
        Path(urllib.parse.urlparse(response.geturl() or url).path).name)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(raw).name).strip("._") or "modele"
    return name[-120:]


def unpack(path: Path, folder: Path) -> tuple[Path | None, tuple[Path, Path] | None]:
    """Fichier modèle à convertir, ou paire ncnn (.param, .bin) déjà prête."""
    suffix = path.suffix.lower()
    if suffix in MODEL_EXTENSIONS:
        return path, None
    if not zipfile.is_zipfile(path):
        if path.read_bytes()[:8].startswith(b"PK"):
            raise InstallError("Archive zip illisible")
        raise InstallError("Format non reconnu : .pth, .safetensors, .onnx, .zip ou .param + .bin attendus")
    folder.mkdir(parents=True, exist_ok=True)
    found: dict[str, list[Path]] = {}
    with zipfile.ZipFile(path) as archive:
        total = 0
        for member in archive.infolist():
            ext = Path(member.filename).suffix.lower()
            if member.is_dir() or ext not in MODEL_EXTENSIONS | {".param", ".bin"}:
                continue
            total += member.file_size
            if total > config.MODEL_MAX_BYTES:
                raise InstallError("Archive trop volumineuse")
            target = folder / f"{len(found.get(ext, []))}{ext}"  # nom maîtrisé : pas de chemin hors du dossier
            with archive.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            found.setdefault(ext, []).append(target)
    if found.get(".param") and found.get(".bin"):
        if len(found[".param"]) > 1 or len(found[".bin"]) > 1:
            raise InstallError("L'archive contient plusieurs modèles ncnn : importe-les un par un")
        return None, (found[".param"][0], found[".bin"][0])
    for ext in (".safetensors", ".pth", ".pt", ".ckpt", ".onnx"):
        if found.get(ext):
            if len(found[ext]) > 1:
                raise InstallError("L'archive contient plusieurs modèles : importe-les un par un")
            return found[ext][0], None
    raise InstallError("Aucun modèle dans l'archive")


# ---------------------------------------------------------------- convertisseur

def converter_python() -> Path:
    if config.CONVERTER_PYTHON:
        return Path(config.CONVERTER_PYTHON)
    return config.converter_dir() / "bin" / "python"


def converter_status() -> dict[str, Any]:
    python = converter_python()
    return {"installed": python.exists(), "path": str(config.converter_dir()),
            "managed": not config.CONVERTER_PYTHON}


def _converter_ok(python: Path) -> bool:
    if not python.exists():
        return False
    try:
        return subprocess.run([str(python), "-I", "-c", "import torch, spandrel, pnnx, numpy"],
                              capture_output=True, timeout=300).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def stream(task: dict[str, Any], command: list[str], *, timeout: float, on_line: Callable[[str], None] | None = None,
           cwd: Path | None = None) -> tuple[int, list[str]]:
    lines: list[str] = []
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace", cwd=cwd)
    except OSError as exc:
        raise InstallError(f"Impossible de lancer {Path(command[0]).name} : {exc}") from None
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip())
            if on_line:
                on_line(line)
        code = proc.wait()
    finally:
        timer.cancel()
    return code, lines[-80:]


def ensure_converter(task: dict[str, Any]) -> Path:
    python = converter_python()
    if _converter_ok(python):
        return python
    if config.CONVERTER_PYTHON:
        raise InstallError(f"Convertisseur {python} inutilisable (torch, spandrel et pnnx requis)")
    folder = config.converter_dir()
    folder.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(folder.parent).free < CONVERTER_FREE_BYTES:
        raise InstallError("Pas assez d'espace pour installer le convertisseur (3 Go libres requis dans le "
                           "dossier d'état)")
    _step(task, "Installation du convertisseur (une seule fois, environ 1 Go à télécharger)")
    shutil.rmtree(folder, ignore_errors=True)
    code, lines = stream(task, [sys.executable, "-m", "venv", str(folder)], timeout=600)
    if code != 0:
        raise InstallError("Création de l'environnement du convertisseur impossible : " + " ".join(lines[-3:]))
    pip = [str(python), "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "--progress-bar", "off"]
    show = lambda line: _log(task, line) if line.startswith(("Collecting", "Downloading", "Successfully", "ERROR")) else None  # noqa: E731
    for args in ([*pip, "--index-url", config.TORCH_INDEX, *TORCH_PACKAGES], [*pip, *CONVERTER_PACKAGES]):
        code, lines = stream(task, args, timeout=3600, on_line=show)
        if code != 0:
            shutil.rmtree(folder, ignore_errors=True)
            raise InstallError("Installation du convertisseur impossible (accès Internet ?) : " + " ".join(lines[-3:]))
    if not _converter_ok(python):
        shutil.rmtree(folder, ignore_errors=True)
        raise InstallError("Convertisseur installé mais inutilisable")
    _log(task, "Convertisseur prêt")
    return python


def make_test_image(folder: Path) -> tuple[Path, Path]:
    """Image de test (motifs, dégradés, texte) en PNG et en RGB24 brut."""
    folder.mkdir(parents=True, exist_ok=True)
    png, raw = folder / "test.png", folder / "test.rgb"
    width, height = TEST_SIZE
    for target, fmt in ((png, ["-frames:v", "1"]), (raw, ["-frames:v", "1", "-f", "rawvideo"])):
        result = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
                                 "-i", f"testsrc2=size={width}x{height}:rate=1", *fmt, "-pix_fmt", "rgb24",
                                 str(target)], capture_output=True, text=True)
        if result.returncode != 0:
            raise InstallError("Création de l'image de test impossible : " + result.stderr[-300:])
    return png, raw


def run_converter(task: dict[str, Any], python: Path, model_file: Path, out: Path, test_raw: Path) -> dict[str, Any]:
    width, height = TEST_SIZE
    command = [str(python), "-I", str(CONVERT_SCRIPT), str(model_file), str(out), "--ref-in", str(test_raw),
               "--ref-size", f"{width}x{height}", "--ref-out", str(out / "reference.rgb")]
    code, lines = stream(task, command, timeout=3600, on_line=lambda line: _log(task, line), cwd=out.parent)
    result: dict[str, Any] = {}
    for line in reversed(lines):
        if line.startswith("{"):
            try:
                result = json.loads(line)
                break
            except ValueError:
                continue
    if code != 0 or not result.get("ok"):
        raise InstallError(result.get("error") or "Conversion impossible : " + " ".join(lines[-4:])[-500:])
    return result


# ---------------------------------------------------------------- vérification

def read_rgb(png: Path) -> tuple[int, int, bytes]:
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "json", str(png)], capture_output=True, text=True)
    try:
        stream_info = json.loads(probe.stdout)["streams"][0]
        width, height = int(stream_info["width"]), int(stream_info["height"])
    except (ValueError, KeyError, IndexError):
        raise InstallError("Image produite par le moteur illisible") from None
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(png), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True)
    return width, height, raw.stdout


def psnr(a: bytes, b: bytes, width: int, height: int, margin: int) -> float:
    """PSNR (dB) sur l'intérieur de l'image : les bords dépendent du remplissage, différent selon le moteur."""
    total, count = 0, 0
    for y in range(margin, height - margin):
        start, end = (y * width + margin) * 3, (y * width + width - margin) * 3
        row_a, row_b = a[start:end], b[start:end]
        total += sum((x - z) * (x - z) for x, z in zip(row_a, row_b, strict=False))
        count += end - start
    if not count:
        return 0.0
    mse = total / count
    return 99.0 if mse == 0 else round(10 * math.log10(255 * 255 / mse), 1)


def verify(task: dict[str, Any], param: Path, binary: Path, scale: int, tile: int, folder: Path,
           reference: Path | None, ref_size: tuple[int, int] | None) -> float | None:
    """Fait tourner le modèle converti dans le moteur Vulkan. Renvoie le PSNR face à PyTorch (si connu)."""
    if not Path(config.NCNN_BIN).exists():
        _log(task, "Moteur Vulkan absent (environnement de développement) : vérification ignorée")
        return None
    _step(task, "Vérification avec le moteur Vulkan")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        text = models.normalize_param(param.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InstallError(f"Modèle ncnn invalide : {exc}") from None
    (folder / f"{models.NCNN_NAME}.param").write_text(text, encoding="utf-8")
    shutil.copyfile(binary, folder / f"{models.NCNN_NAME}.bin")
    png, _ = make_test_image(folder / "entree")
    out = folder / "sortie.png"
    meta = {"folder": str(folder), "scale": scale, "tile": tile}
    from . import runner  # import local : runner importe models

    command = runner.ncnn_command(png, out, meta)
    started = time.monotonic()
    code, lines = stream(task, command, timeout=600)
    if code != 0 or not out.exists():
        unknown = [line for line in lines if "not exists or registered" in line or "layer" in line.lower()]
        hint = (" Couche inconnue du moteur : " + unknown[0]) if unknown else ""
        raise InstallError("Le moteur Vulkan ne sait pas exécuter ce modèle (architecture non prise en charge)."
                           + hint + (" " + " ".join(lines[-3:]) if not hint else ""))
    width, height, pixels = read_rgb(out)
    expected = (TEST_SIZE[0] * scale, TEST_SIZE[1] * scale)
    if (width, height) != expected:
        raise InstallError(f"Le modèle produit {width}×{height} au lieu de {expected[0]}×{expected[1]} : facteur ×"
                           f"{scale} incorrect ?")
    _log(task, f"Moteur Vulkan : {width}×{height} en {time.monotonic() - started:.1f} s")
    if not reference or not ref_size or tuple(ref_size) != expected:
        return None
    value = psnr(pixels, reference.read_bytes(), width, height, margin=12 * scale)
    _log(task, f"Écart avec le modèle PyTorch d'origine : PSNR {value} dB")
    if value < MIN_PSNR:
        raise InstallError(f"Conversion incorrecte : le résultat du moteur diffère de celui du modèle d'origine "
                           f"(PSNR {value} dB < {MIN_PSNR}). Cette architecture n'est pas utilisable ici.")
    return value
