"""Modèles personnalisés (format ncnn .param + .bin), gérés depuis l'interface.

Video2X n'accepte que ses trois modèles intégrés : les modèles personnalisés passent par le moteur
realesrgan-ncnn-vulkan (Vulkan, compatible AMD). Architectures prises en charge : celles que ce moteur sait
exécuter, en pratique ESRGAN (RRDBNet) et Compact (SRVGGNet), converties au format ncnn par exemple avec
chaiNNer (« Convert to NCNN »).
"""
from __future__ import annotations

import json
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any

from . import config

PREFIX = "perso-"
# Nom des fichiers sur disque : realesrgan-ncnn-vulkan refuse les noms de modèle qu'il ne connaît pas
# (« unknown model dir type ») ; ce nom est accepté, et le facteur reste celui passé en -s.
NCNN_NAME = "realesrgan-x4plus-perso"
NCNN_MAGIC = "7767517"  # première ligne de tout fichier .param ncnn
SCALES = (2, 3, 4)
TILES = (0, 512, 256, 128, 64)
MAX_LABEL = 60
ID_RE = re.compile(r"^perso-[a-z0-9-]{1,48}$")


def models_dir() -> Path:
    return config.STATE_DIR / "models"


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
        raise ValueError("Le fichier .param n'est pas un modèle ncnn (convertis le .pth avec chaiNNer → NCNN)")


def create(label: Any, scale: Any, tile: Any, param_stream: Any, bin_stream: Any) -> dict[str, Any]:
    """Enregistre un modèle importé. Les flux sont des fichiers ouverts en lecture binaire."""
    label, scale, tile = _validate_meta(label, scale, tile)
    base = PREFIX + slugify(label)
    model_id, index = base, 2
    while (models_dir() / model_id).exists():
        model_id, index = f"{base}-{index}", index + 1
    folder = _folder(model_id)
    temp = folder.with_name(folder.name + ".import")
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True)
    try:
        param = temp / f"{NCNN_NAME}.param"
        with param.open("wb") as out:
            shutil.copyfileobj(param_stream, out)
        _check_param(param.read_bytes()[:64])
        binary = temp / f"{NCNN_NAME}.bin"
        with binary.open("wb") as out:
            shutil.copyfileobj(bin_stream, out)
        if binary.stat().st_size < 1024:
            raise ValueError("Le fichier .bin est vide ou incomplet")
        meta = {"id": model_id, "label": label, "scale": scale, "tile": tile,
                "param_size": param.stat().st_size, "bin_size": binary.stat().st_size}
        (temp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        temp.rename(folder)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return get(model_id) or meta


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
