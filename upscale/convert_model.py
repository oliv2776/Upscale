"""Conversion d'un modèle d'agrandissement (PyTorch .pth/.safetensors ou ONNX) au format ncnn.

Script autonome : il est exécuté par l'interpréteur du convertisseur (environnement séparé avec torch,
spandrel et pnnx, installé à la première conversion), jamais importé par l'application.

Les fichiers .pth sont lus par spandrel avec un dépickleur restreint (aucun code arbitraire exécuté).

Résultat : <sortie>/model.param, <sortie>/model.bin et <sortie>/info.json. Si --ref-in est donné (image RGB24
brute de --ref-size LxH), le modèle PyTorch d'origine l'agrandit dans --ref-out : l'application compare ensuite
cette référence au résultat du moteur Vulkan pour vérifier la conversion.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def fail(message: str) -> None:
    print(json.dumps({"ok": False, "error": message}), flush=True)
    sys.exit(2)


def pnnx_binary() -> str:
    import pnnx  # noqa: PLC0415

    return pnnx.EXEC_PATH


def run_pnnx(model_file: Path, work: Path, shape1: tuple[int, int], shape2: tuple[int, int]) -> tuple[Path, Path]:
    command = [pnnx_binary(), str(model_file), f"inputshape=[1,3,{shape1[1]},{shape1[0]}]f32",
               f"inputshape2=[1,3,{shape2[1]},{shape2[0]}]f32", "device=cpu", "fp16=1",
               f"ncnnparam={work / 'model.param'}", f"ncnnbin={work / 'model.bin'}",
               f"pnnxparam={work / 'pnnx.param'}", f"pnnxbin={work / 'pnnx.bin'}",
               f"pnnxpy={work / 'pnnx.py'}", f"pnnxonnx={work / 'pnnx.onnx'}", f"ncnnpy={work / 'ncnn.py'}"]
    print("pnnx:", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=work, capture_output=True, text=True, errors="replace")
    tail = "\n".join((result.stdout + result.stderr).splitlines()[-25:])
    print(tail, flush=True)
    param, binary = work / "model.param", work / "model.bin"
    if result.returncode != 0 or not param.exists() or not binary.exists():
        raise RuntimeError(f"pnnx a échoué (code {result.returncode}) : {tail[-600:]}")
    return param, binary


def input_sizes(multiple: int, minimum: int) -> tuple[tuple[int, int], tuple[int, int]]:
    def fit(value: int) -> int:
        value = max(value, minimum)
        return -(-value // multiple) * multiple

    # Deux tailles différentes : pnnx en déduit des formes dynamiques (le moteur découpe en tuiles variables).
    return (fit(64), fit(48)), (fit(96), fit(80))


def load_torch(path: Path):
    import spandrel  # noqa: PLC0415

    try:
        descriptor = spandrel.ModelLoader().load_from_file(str(path))
    except spandrel.UnsupportedModelError:
        fail("Architecture inconnue de spandrel : ce fichier n'est pas un modèle d'agrandissement pris en charge")
    except Exception as exc:  # noqa: BLE001 - fichier corrompu, format inattendu...
        fail(f"Lecture du modèle impossible : {exc}")
    if not isinstance(descriptor, spandrel.ImageModelDescriptor):
        fail("Ce modèle ne transforme pas une image en image (inpainting, etc.) : inutilisable ici")
    return descriptor


def convert_torch(path: Path, work: Path) -> tuple[dict, object]:
    import torch  # noqa: PLC0415

    descriptor = load_torch(path)
    info = {"architecture": descriptor.architecture.name, "scale": int(descriptor.scale),
            "input_channels": int(descriptor.input_channels), "output_channels": int(descriptor.output_channels),
            "purpose": descriptor.purpose}
    print("Modèle :", json.dumps(info), flush=True)
    if info["input_channels"] != 3 or info["output_channels"] != 3:
        fail(f"Modèle {info['input_channels']}→{info['output_channels']} canaux : seuls les modèles couleur (RGB) "
             "sont utilisables")
    if info["scale"] not in (2, 3, 4):
        fail(f"Modèle ×{info['scale']} : le moteur Vulkan ne prend en charge que les facteurs ×2, ×3 et ×4")
    req = descriptor.size_requirements
    multiple, minimum = int(getattr(req, "multiple_of", 1) or 1), int(getattr(req, "minimum", 0) or 0)
    if multiple > 1 and not getattr(req, "square", False):
        print(f"Avertissement : dimensions multiples de {multiple} attendues ; le modèle complète lui-même "
              "les bords (vérifié ci-dessous)", flush=True)
    if getattr(req, "square", False):
        fail("Ce modèle n'accepte que des images carrées : incompatible avec l'agrandissement par tuiles")
    net = descriptor.model.eval().float()
    shape1, shape2 = input_sizes(multiple, minimum)
    errors = []
    # 1) TorchScript -> pnnx (voie normale) ; 2) ONNX -> pnnx si le traçage TorchScript ne passe pas.
    with torch.no_grad():
        try:
            traced = torch.jit.trace(net, torch.rand(1, 3, shape1[1], shape1[0]), check_trace=False)
            pt = work / "model.pt"
            traced.save(str(pt))
            run_pnnx(pt, work, shape1, shape2)
            return info, net
        except Exception as exc:  # noqa: BLE001
            errors.append(f"TorchScript : {exc}")
            print(errors[-1], flush=True)
        try:
            onnx_path = work / "model.onnx"
            sample = torch.rand(1, 3, shape1[1], shape1[0])
            kwargs = {"input_names": ["input"], "output_names": ["output"], "opset_version": 17,
                      "dynamic_axes": {"input": {2: "h", 3: "w"}, "output": {2: "oh", 3: "ow"}}}
            try:
                torch.onnx.export(net, sample, str(onnx_path), dynamo=False, **kwargs)
            except TypeError:  # anciennes versions de torch sans l'argument dynamo
                torch.onnx.export(net, sample, str(onnx_path), **kwargs)
            run_pnnx(onnx_path, work, shape1, shape2)
            return info, net
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ONNX : {exc}")
    fail("Conversion en ncnn impossible pour l'architecture " + info["architecture"] + " — " + " | ".join(errors))
    raise AssertionError  # inatteignable


def reference(net, ref_in: Path, size: str, ref_out: Path) -> list[int]:
    import numpy as np  # noqa: PLC0415
    import torch  # noqa: PLC0415

    width, height = (int(v) for v in size.lower().split("x"))
    pixels = np.frombuffer(ref_in.read_bytes(), dtype=np.uint8).reshape(height, width, 3)
    tensor = torch.from_numpy(pixels.astype(np.float32) / 255.0).permute(2, 0, 1).unsqueeze(0)
    with torch.no_grad():
        out = net(tensor).clamp(0, 1)
    result = (out[0].permute(1, 2, 0).numpy() * 255.0).round().astype(np.uint8)
    ref_out.write_bytes(result.tobytes())
    return [int(result.shape[1]), int(result.shape[0])]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ref-in", type=Path)
    parser.add_argument("--ref-size")
    parser.add_argument("--ref-out", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    work = args.output / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    suffix = args.source.suffix.lower()
    net = None
    if suffix == ".onnx":
        info = {"architecture": "onnx"}
        try:
            run_pnnx(args.source, work, (64, 48), (96, 80))
        except RuntimeError as exc:
            fail(f"Conversion ONNX impossible : {exc}")
    elif suffix in {".pth", ".pt", ".ckpt", ".safetensors"}:
        info, net = convert_torch(args.source, work)
    else:
        fail("Format non pris en charge : .pth, .safetensors, .ckpt ou .onnx attendu")
    shutil.move(str(work / "model.param"), args.output / "model.param")
    shutil.move(str(work / "model.bin"), args.output / "model.bin")
    if net is not None and args.ref_in and args.ref_size and args.ref_out:
        info["reference_size"] = reference(net, args.ref_in, args.ref_size, args.ref_out)
    shutil.rmtree(work, ignore_errors=True)
    (args.output / "info.json").write_text(json.dumps(info), encoding="utf-8")
    print(json.dumps({"ok": True, **info}), flush=True)


if __name__ == "__main__":
    main()
