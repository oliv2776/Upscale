"""Fonctions et constantes partagées par les tests."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from upscale import config, db, pipeline, runner, scanner, worker
from upscale import settings as st


def insert_job(job_id: str, status: str = "queued", position: float = 1.0, **extra) -> None:
    with db.connect() as conn:
        conn.execute("""INSERT INTO jobs(id,source,destination,source_size,source_mtime_ns,status,stage,
                        created_at,updated_at,position,kind,priority) VALUES(?,?,?,1,?,?,'',?,?,?,?,?)""",
                     (job_id, f"/s/{job_id}", f"/d/{job_id}.mkv", position, status, db.now(), db.now(), position,
                      extra.get("kind", "upscale"), extra.get("priority", 0)))


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


root_only = pytest.mark.skipif(os.geteuid() != 0, reason="chown vers un autre utilisateur : root requis")


def add_disk(name: str) -> Path:
    disk = config.DISKS_ROOT / name
    disk.mkdir(parents=True)
    return disk.resolve()


TINY_PARAM = b"7767517\n2 2\nInput in0 0 1 in0\nConvolution conv0 1 1 in0 out0 0=3\n"


def import_model(client, label="Peau réaliste 2x", scale=2, param=TINY_PARAM):
    return client.post("/api/models", headers={"X-Upscale-Upload": "1"}, content_type="multipart/form-data", data={
        "label": label, "scale": str(scale), "tile": "0",
        "param": (io.BytesIO(param), "modele.param"), "bin": (io.BytesIO(b"\0" * 4096), "modele.bin")})


PNNX_PARAM = "7767517\n3 3\nInput in0 0 1 in0\nConvolution conv_0 1 1 in0 1 0=3\nPixelShuffle ps_1 1 1 1 out0 0=2\n"


def omdb_entry(base: str, payload: bytes, **extra):
    return {"name": "2xTestLive_SPAN", "author": "jcj83429", "license": "CC-BY-NC-SA-4.0", "architecture": "span",
            "scale": 2, "inputChannels": 3, "outputChannels": 3,
            "resources": [{"platform": "pytorch", "type": "pth", "size": len(payload),
                           "sha256": hashlib.sha256(payload).hexdigest(),
                           "urls": ["https://drive.google.com/drive/folders/inutilisable", f"{base}/files/2x_live.pth"]}],
            **extra}


def _patch_rgb(path: Path, matrix: str, x: float, y: float) -> tuple[int, int, int]:
    """Couleur moyenne d'un carré de l'image, décodée comme le ferait un lecteur avec cette matrice."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf",
         f"scale=in_color_matrix={matrix},format=rgb24,crop=16:16:iw*{x}-8:ih*{y}-8", "-f", "rawvideo", "-"],
        capture_output=True, check=True).stdout
    return tuple(sum(raw[c::3]) // (len(raw) // 3) for c in range(3))
