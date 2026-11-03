"""Modèles personnalisés (format ncnn .param + .bin), gérés depuis l'interface.

Video2X n'accepte que ses trois modèles intégrés : les modèles personnalisés passent par le moteur
ncnn upscayl-bin (fork de realesrgan-ncnn-vulkan, Vulkan, compatible AMD). Ils arrivent soit déjà au format
ncnn, soit en PyTorch/ONNX convertis sur place par converter.py (OpenModelDB, fichier .pth...).
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from . import config

PREFIX = "perso-"
# Nom des fichiers sur disque. Sans chiffre de facteur : upscayl-bin déduit le facteur d'un nom contenant
# « x2 », « 4x »... Le moteur exige aussi que le chemin du dossier contienne « models » (marge entre tuiles,
# et « unknown model dir type » pour l'ancien moteur) : c'est garanti par models_dir().
NCNN_NAME = "modele"
LEGACY_NCNN_NAMES = ("realesrgan-x4plus-perso",)  # versions ≤ 2.6
NCNN_MAGIC = "7767517"  # première ligne de tout fichier .param ncnn
SCALES = (2, 3, 4)
TILES = (0, 512, 256, 128, 64)
MAX_LABEL = 60
ID_RE = re.compile(r"^perso-[a-z0-9-]{1,48}$")


def models_dir() -> Path:
    return config.STATE_DIR / "models"  # le nom « models » est exigé par le moteur


def is_custom(model: Any) -> bool:
    return isinstance(model, str) and model.startswith(PREFIX)


def _folder(model_id: str) -> Path:
    if not ID_RE.match(model_id):
        raise ValueError("Identifiant de modèle invalide")
    return models_dir() / model_id


def get(model_id: str) -> dict[str, Any] | None:
    try:
        folder = _folder(model_id)
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    for legacy in LEGACY_NCNN_NAMES:  # renommage des fichiers des versions précédentes
        for ext in (".param", ".bin"):
            old = folder / f"{legacy}{ext}"
            if old.exists() and not (folder / f"{NCNN_NAME}{ext}").exists():
                old.rename(folder / f"{NCNN_NAME}{ext}")
    if not (folder / f"{NCNN_NAME}.param").exists() or not (folder / f"{NCNN_NAME}.bin").exists():
        return None
    meta["folder"] = str(folder)
    return meta


def list_models() -> list[dict[str, Any]]:
    result = []
    root = models_dir()
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            meta = get(folder.name) if folder.is_dir() else None
            if meta:
                result.append(meta)
    return result


def slugify(label: str) -> str:
    text = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40] or "modele"


def _validate_meta(label: Any, scale: Any, tile: Any) -> tuple[str, int, int]:
    label = str(label or "").strip()
    if not label or len(label) > MAX_LABEL:
        raise ValueError(f"Nom du modèle requis ({MAX_LABEL} caractères max.)")
    try:
        scale, tile = int(scale), int(tile or 0)
    except (TypeError, ValueError):
        raise ValueError("Facteur ou taille de tuile invalide") from None
    if scale not in SCALES:
        raise ValueError("Facteur d'échelle : 2, 3 ou 4 (celui pour lequel le modèle a été entraîné)")
    if tile not in TILES:
        raise ValueError("Taille de tuile invalide")
    return label, scale, tile


def _check_param(head: bytes) -> None:
    first = head.split(b"\n", 1)[0].strip().decode("ascii", "ignore")
    if first != NCNN_MAGIC:
        raise ValueError("Le fichier .param n'est pas un modèle ncnn : importe plutôt le .pth ou le .onnx, "
                         "Upscale le convertit lui-même")


def normalize_param(text: str) -> str:
    """Renomme l'entrée et la sortie du réseau en « data » / « output », les noms qu'utilise le moteur.

    pnnx produit « in0 » / « out0 », d'autres convertisseurs « input »... Format d'une ligne de couche :
    type nom nb_entrées nb_sorties entrées... sorties... paramètres...
    """
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != NCNN_MAGIC:
        raise ValueError("Fichier .param ncnn invalide")
    layers = []
    produced: list[str] = []
    consumed: set[str] = set()
    inputs: list[str] = []
    for line in lines[2:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            count_in, count_out = int(parts[2]), int(parts[3])
        except ValueError:
            raise ValueError("Fichier .param ncnn invalide") from None
        blobs_in, blobs_out = parts[4:4 + count_in], parts[4 + count_in:4 + count_in + count_out]
        consumed.update(blobs_in)
        produced.extend(blobs_out)
        if parts[0] == "Input":
            inputs.extend(blobs_out)
        layers.append((parts, count_in, count_out))
    outputs = [blob for blob in produced if blob not in consumed]
    if len(inputs) != 1 or len(outputs) != 1 or inputs == outputs:
        raise ValueError(f"Le modèle doit avoir une entrée et une sortie (trouvé {len(inputs)} entrée(s), "
                         f"{len(outputs)} sortie(s))")
    mapping = {inputs[0]: "data", outputs[0]: "output"}
    others = set(produced) - set(mapping)
    for reserved in ("data", "output"):
        if reserved in others:  # nom déjà pris par un blob intermédiaire
            mapping[reserved] = f"{reserved}_interne"
    result = lines[:2]
    for parts, count_in, count_out in layers:
        span = 4 + count_in + count_out
        renamed = [mapping.get(blob, blob) for blob in parts[4:span]]
        result.append(" ".join(parts[:4] + renamed + parts[span:]))
    return "\n".join(result) + "\n"


def _new_id(label: str) -> str:
    base = PREFIX + slugify(label)
    model_id, index = base, 2
    while (models_dir() / model_id).exists() or (models_dir() / f"{model_id}.import").exists():
        model_id, index = f"{base}-{index}", index + 1
    return model_id


def create_from_files(label: Any, scale: Any, tile: Any, param_path: Path, bin_path: Path,
                      extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Enregistre un modèle ncnn à partir de deux fichiers (copiés) ; extra = provenance, licence..."""
    label, scale, tile = _validate_meta(label, scale, tile)
    raw = Path(param_path).read_bytes()
    _check_param(raw[:64])
    try:
        param_text = normalize_param(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ValueError("Fichier .param ncnn invalide") from None
    if Path(bin_path).stat().st_size < 1024:
        raise ValueError("Le fichier .bin est vide ou incomplet")
    model_id = _new_id(label)
    folder = _folder(model_id)
    temp = folder.with_name(folder.name + ".import")
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True)
    try:
        param = temp / f"{NCNN_NAME}.param"
        param.write_text(param_text, encoding="utf-8")
        binary = temp / f"{NCNN_NAME}.bin"
        shutil.copyfile(bin_path, binary)
        meta = {"id": model_id, "label": label, "scale": scale, "tile": tile,
                "param_size": param.stat().st_size, "bin_size": binary.stat().st_size, **(extra or {})}
        (temp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        temp.rename(folder)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return get(model_id) or meta


def create(label: Any, scale: Any, tile: Any, param_stream: Any, bin_stream: Any) -> dict[str, Any]:
    """Enregistre un modèle ncnn envoyé depuis l'interface (flux ouverts en lecture binaire)."""
    _validate_meta(label, scale, tile)
    models_dir().mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".envoi-", dir=models_dir()))
    try:
        param, binary = staging / "model.param", staging / "model.bin"
        with param.open("wb") as out:
            shutil.copyfileobj(param_stream, out)
        with binary.open("wb") as out:
            shutil.copyfileobj(bin_stream, out)
        return create_from_files(label, scale, tile, param, binary, {"source": {"kind": "ncnn"}})
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def update(model_id: str, label: Any, scale: Any, tile: Any) -> dict[str, Any]:
    meta = get(model_id)
    if not meta:
        raise LookupError("Modèle introuvable")
    label, scale, tile = _validate_meta(label, scale, tile)
    meta.update(label=label, scale=scale, tile=tile)
    meta.pop("folder", None)
    (_folder(model_id) / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return get(model_id) or meta


def delete(model_id: str) -> None:
    if not get(model_id):
        raise LookupError("Modèle introuvable")
    shutil.rmtree(_folder(model_id))
