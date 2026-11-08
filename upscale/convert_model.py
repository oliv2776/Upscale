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


def _limit_resources() -> None:
    """Limite les dégâts d'un modèle malveillant : temps CPU borné, pas de core dump."""
    try:
        import resource
    except ImportError:
        return
    for limit, value in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_CPU, 7200)):
        try:
            resource.setrlimit(limit, (value, value))
        except (OSError, ValueError):
            pass


_limit_resources()


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
    if result.returncode in (-9, 137):
        raise RuntimeError("pnnx arrêté faute de mémoire (modèle trop gros pour être converti ici)")
    if result.returncode != 0 or not param.exists() or not binary.exists():
        raise RuntimeError(f"pnnx a échoué (code {result.returncode}) : {tail[-600:]}")
    return param, binary


# ---------------------------------------------------------------- écriture ncnn directe (sans pnnx)
# Pour les architectures simples et très répandues (ESRGAN) : pnnx y consomme énormément de mémoire, alors que
# le réseau ne contient que des convolutions, concaténations, sommes et agrandissements.

NATIVE_ARCHITECTURES = {"ESRGAN"}


class Unsupported(Exception):
    pass


class NcnnWriter:
    def __init__(self) -> None:
        self.layers: list[list] = []  # [type, nom, entrées, sorties, paramètres]
        self.weights: list[bytes] = []
        self.count = 0

    def blob(self) -> str:
        self.count += 1
        return f"b{self.count}"

    def add(self, kind: str, inputs: list[str], params: str = "", weights: bytes = b"") -> str:
        out = self.blob()
        self.layers.append([kind, f"{kind.lower()}_{len(self.layers)}", list(inputs), [out], params])
        if weights:
            self.weights.append(weights)
        return out

    def module(self, mod, x: str) -> str:  # noqa: PLR0911, PLR0912 - une branche par type de couche
        import numpy as np  # noqa: PLC0415
        import torch.nn as nn  # noqa: PLC0415

        name = type(mod).__name__
        if isinstance(mod, nn.Sequential):
            for child in mod:
                x = self.module(child, x)
            return x
        if isinstance(mod, nn.Identity):
            return x
        if isinstance(mod, nn.Conv2d):
            if mod.groups != 1 or mod.padding_mode != "zeros" or isinstance(mod.padding, str):
                raise Unsupported("convolution groupée ou remplissage non nul")
            kh, kw = mod.kernel_size
            sh, sw = mod.stride
            dh, dw = mod.dilation
            ph, pw = mod.padding
            weight = mod.weight.detach().float().cpu().numpy().astype("<f4")
            data = np.zeros(1, dtype="<u4").tobytes() + weight.tobytes()  # 0 : float32 brut
            bias = mod.bias is not None
            if bias:
                data += mod.bias.detach().float().cpu().numpy().astype("<f4").tobytes()
            params = (f"0={mod.out_channels} 1={kw} 11={kh} 2={dw} 12={dh} 3={sw} 13={sh} 4={pw} 15={pw} "
                      f"14={ph} 16={ph} 5={int(bias)} 6={weight.size}")
            return self.add("Convolution", [x], params, data)
        if isinstance(mod, nn.LeakyReLU):
            return self.add("ReLU", [x], f"0={mod.negative_slope:e}")
        if isinstance(mod, nn.ReLU):
            return self.add("ReLU", [x])
        if isinstance(mod, nn.PReLU):
            slope = mod.weight.detach().float().cpu().numpy().astype("<f4")
            return self.add("PReLU", [x], f"0={slope.size}", slope.tobytes())
        if isinstance(mod, nn.Sigmoid):
            return self.add("Sigmoid", [x])
        if isinstance(mod, nn.SiLU):
            return self.add("Swish", [x])
        if isinstance(mod, nn.PixelShuffle):
            return self.add("PixelShuffle", [x], f"0={mod.upscale_factor}")
        if isinstance(mod, nn.Upsample):
            if mod.mode != "nearest" or mod.size is not None:
                raise Unsupported(f"agrandissement {mod.mode}")
            factor = float(mod.scale_factor if not isinstance(mod.scale_factor, tuple) else mod.scale_factor[0])
            return self.add("Interp", [x], f"0=1 1={factor:e} 2={factor:e}")
        if name == "ShortcutBlock":
            return self.add("BinaryOp", [x, self.module(mod.sub, x)], "0=0")
        if name == "RRDB":
            out = x
            for block in (mod.RDB1, mod.RDB2, mod.RDB3):
                out = self.module(block, out)
            return self.residual(out, x)
        if name == "ResidualDenseBlock_5C":
            x1 = self.module(mod.conv1, x)
            x2 = self.module(mod.conv2, self.add("Concat", [x, x1], "0=0"))
            if mod.conv1x1 is not None:
                x2 = self.add("BinaryOp", [x2, self.module(mod.conv1x1, x)], "0=0")
            x3 = self.module(mod.conv3, self.add("Concat", [x, x1, x2], "0=0"))
            x4 = self.module(mod.conv4, self.add("Concat", [x, x1, x2, x3], "0=0"))
            if mod.conv1x1 is not None:
                x4 = self.add("BinaryOp", [x4, x2], "0=0")
            x5 = self.module(mod.conv5, self.add("Concat", [x, x1, x2, x3, x4], "0=0"))
            return self.residual(x5, x)
        raise Unsupported(f"couche {name}")

    def residual(self, out: str, x: str) -> str:
        scaled = self.add("BinaryOp", [out], "0=2 1=1 2=2.000000e-01")
        return self.add("BinaryOp", [scaled, x], "0=0")

    def save(self, param: Path, binary: Path, inp: str, out: str) -> None:
        # ncnn exige une couche Split quand un blob est lu par plusieurs couches.
        readers: dict[str, int] = {}
        for layer in self.layers:
            for blob in layer[2]:
                readers[blob] = readers.get(blob, 0) + 1
        layers = [["Input", "input", [], [inp], ""]] + self.layers
        result = []
        for layer in layers:
            result.append(layer)
            for blob in layer[3]:
                count = readers.get(blob, 0)
                if count > 1:
                    copies = [f"{blob}_{i}" for i in range(count)]
                    result.append(["Split", f"split_{blob}", [blob], copies, ""])
                    pending = iter(copies)
                    for later in layers[layers.index(layer) + 1:]:
                        later[2] = [next(pending) if b == blob else b for b in later[2]]
        for layer in result:
            layer[3] = ["output" if b == out else b for b in layer[3]]
            layer[2] = ["output" if b == out else b for b in layer[2]]
        blobs = {b for layer in result for b in layer[3]}
        lines = ["7767517", f"{len(result)} {len(blobs)}"]
        for kind, name, inputs, outputs, params in result:
            lines.append(" ".join([kind, name, str(len(inputs)), str(len(outputs)), *inputs, *outputs, params]).rstrip())
        param.write_text("\n".join(lines) + "\n", encoding="utf-8")
        binary.write_bytes(b"".join(self.weights))


def write_native(descriptor, work: Path) -> None:
    net = descriptor.model
    arch = descriptor.architecture.name
    writer = NcnnWriter()
    if arch == "ESRGAN":
        if getattr(net, "shuffle_factor", None):
            raise Unsupported("variante ESRGAN à pixel-unshuffle")
        body = net.model
    else:
        raise Unsupported(arch)
    out = writer.module(body, "data")
    writer.save(work / "model.param", work / "model.bin", "data", out)
    print(f"Écriture ncnn directe : {len(writer.layers)} couches", flush=True)


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
    if info["architecture"] in NATIVE_ARCHITECTURES:
        try:
            write_native(descriptor, work)
            return info, net
        except Unsupported as exc:
            errors.append(f"écriture directe : {exc}")
            print(errors[-1], flush=True)
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
