"""Modèles personnalisés : import, installation OpenModelDB, conversion."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from conftest import calls, make_video, needs_ffmpeg
from helpers import (
    PNNX_PARAM,
    TINY_PARAM,
    import_model,
    job_log,
    omdb_entry,
    process_all,
    video_stream,
)

from upscale import config, converter, db, media, models, net, ollama, pipeline, worker
from upscale import settings as st
from upscale.web import app


def test_retired_model_is_migrated(env):
    stored = st.get_settings()
    db.store_settings_payload({**stored, "model": "realesr-generalv3",
                               "profiles": [{"path": str(env / "videos"), "model": "realesr-generalv3"}]})
    settings = st.get_settings()
    assert settings["model"] == "realesrgan-plus" and settings["profiles"][0]["model"] == "realesrgan-plus"
    assert st.save_settings({"model": "realesr-generalv3"})["model"] == "realesrgan-plus"


def test_custom_model_management(env):
    client = app.test_client()
    bad = import_model(client, param=b"pas un modele")
    assert bad.status_code == 400 and "ncnn" in bad.get_json()["error"]
    no_header = client.post("/api/models", content_type="multipart/form-data", data={"label": "x"})
    assert no_header.status_code == 415  # import impossible depuis un site tiers (CSRF)
    created = import_model(client)
    assert created.status_code == 201
    model_id = created.get_json()["model"]["id"]
    assert model_id == "perso-peau-realiste-2x"
    listed = client.get("/api/config").get_json()["models"]
    assert listed[model_id]["custom"] is True and listed[model_id]["scales"] == [2]
    assert media.choose_scale(model_id, 240, 720) == 2  # facteur unique du modèle
    assert import_model(client).get_json()["model"]["id"] == "perso-peau-realiste-2x-2"
    assert client.post(f"/api/models/{model_id}/update", json={"label": "Peau v2", "scale": 4, "tile": 256}).status_code == 200
    assert models.get(model_id)["label"] == "Peau v2" and models.get(model_id)["tile"] == 256
    assert client.post(f"/api/models/{model_id}/update", json={"label": "x", "scale": 5}).status_code == 400
    st.save_settings({"model": model_id})
    assert client.post(f"/api/models/{model_id}/delete", json={}).status_code == 400  # encore utilisé
    st.save_settings({"model": "realesrgan-plus"})
    assert client.post(f"/api/models/{model_id}/delete", json={}).status_code == 200
    with pytest.raises(ValueError):
        st.save_settings({"model": model_id})
    assert client.post("/api/models/..%2f..%2fetc/delete", json={}).status_code in {400, 404}
    legacy = models.models_dir() / "perso-ancien"  # modèle importé par la version 2.6
    legacy.mkdir()
    (legacy / "realesrgan-x4plus-perso.param").write_bytes(TINY_PARAM)
    (legacy / "realesrgan-x4plus-perso.bin").write_bytes(b"\0" * 4096)
    (legacy / "meta.json").write_text(json.dumps({"id": "perso-ancien", "label": "Ancien", "scale": 2, "tile": 0,
                                                  "param_size": 1, "bin_size": 4096}))
    assert models.get("perso-ancien")["scale"] == 2
    assert (legacy / "modele.param").exists() and not (legacy / "realesrgan-x4plus-perso.param").exists()


@needs_ffmpeg
def test_custom_model_end_to_end_and_preview(env, fake_docker, monkeypatch):
    monkeypatch.setattr(config, "CUSTOM_SEGMENT_SECONDS", 1)
    client = app.test_client()
    model_id = import_model(client).get_json()["model"]["id"]
    st.save_settings({"model": model_id, "render": "custom", "ai_strength": 80, "grain": "medium"})
    source = make_video(env / "videos" / "scene.mp4", seconds=2)
    [row] = process_all(env)
    assert row["status"] == "completed" and row["warning"] is None, job_log(row)
    ncnn_calls = [c for c in calls(fake_docker) if c[0] == "ncnn"]
    assert len(ncnn_calls) == 2 and ncnn_calls[0][ncnn_calls[0].index("-z") + 1] == "2"  # 2 segments de 1 s
    assert "models" in ncnn_calls[0][ncnn_calls[0].index("-m") + 1]  # exigé par le moteur
    assert not any(c[:2] == ["docker", "run"] for c in calls(fake_docker))  # Video2X inutile ici
    out = env / "out" / "scene.mkv"
    assert video_stream(out)["height"] == 720 and abs(media.probe_source(out)["duration"] - 2.0) < 0.3

    response = client.post("/api/preview", json={"source": str(source), "duration": 2, "models": ["realesrgan-plus"],
                                                 "peau_normale": False})
    assert response.status_code == 202
    job = worker.claim_next_job()
    pipeline.run_job(job)
    manifest = client.get("/api/previews").get_json()["previews"][0]["manifest"]
    assert [v["id"] for v in manifest["variants"]] == ["original", "config", "realesrgan-plus"]
    label = manifest["variants"][1]["label"]
    assert "Peau réaliste 2x (personnalisé)" in label and "Personnalisé (80 % IA, grain medium)" in label
    assert "HEVC CRF 16" in label
    log = (config.log_dir() / f"{job['id']}.log").read_text()
    assert "blend=all_expr='A*0.80+B*0.20'" in log and "libx265" in log  # rendu et encodeur appliqués


@needs_ffmpeg
def test_ollama_auto_model(env, http_server):
    base, recorder = http_server
    recorder.responses["/api/generate"] = (200, json.dumps({"response": "ANIMATION"}).encode())
    clip = make_video(env / "videos" / "dessin.mp4", seconds=2)
    settings = {"ollama_enabled": True, "ollama_url": base, "ollama_model": "llava"}
    model, reason = ollama.classify(settings, clip, 2.0)
    assert model == st.ANIMATION_MODEL and "animation" in reason
    request = json.loads(recorder.received[-1]["body"])
    assert request["model"] == "llava" and len(request["images"]) == 3
    recorder.responses["/api/generate"] = (200, json.dumps({"response": "LIVE"}).encode())
    assert ollama.classify(settings, clip, 2.0)[0] == st.LIVE_ACTION_MODEL
    assert ollama.classify({**settings, "ollama_enabled": False}, clip, 2.0)[0] == st.LIVE_ACTION_MODEL


def test_normalize_param_names_engine_blobs():
    text = models.normalize_param(PNNX_PARAM)
    assert "Input in0 0 1 data" in text and "conv_0 1 1 data 1 0=3" in text and "ps_1 1 1 1 output 0=2" in text
    assert models.normalize_param(text) == text  # déjà conforme (chaiNNer) : inchangé
    clash = "7767517\n3 3\nInput input 0 1 input\nConvolution c 1 1 input data\nSigmoid s 1 1 data out\n"
    renamed = models.normalize_param(clash)
    assert "c 1 1 data data_interne" in renamed and "s 1 1 data_interne output" in renamed
    with pytest.raises(ValueError):  # deux sorties
        models.normalize_param("7767517\n3 4\nInput in0 0 1 in0\nSplit sp 1 2 in0 a b\n")
    with pytest.raises(ValueError):
        models.normalize_param("pas ncnn\n")


def test_direct_download_urls():
    drive = converter.direct_url("https://drive.google.com/file/d/1CX9tpoS-OtU/view?usp=drive_link")
    assert drive == "https://drive.usercontent.google.com/download?id=1CX9tpoS-OtU&export=download&confirm=t"
    assert converter.direct_url("https://drive.google.com/drive/folders/1eHak") is None
    assert converter.direct_url("https://mega.nz/file/abc") is None
    assert converter.direct_url("https://github.com/u/r/blob/main/m.pth") == "https://github.com/u/r/raw/main/m.pth"
    assert converter.direct_url("https://huggingface.co/u/r/blob/main/m.pth") == "https://huggingface.co/u/r/resolve/main/m.pth"
    assert converter.direct_url("file:///etc/passwd") is None
    assert converter.openmodeldb_id("https://openmodeldb.info/models/2x-LiveActionV1-SPAN?x=1") == "2x-LiveActionV1-SPAN"
    assert converter.openmodeldb_id("2x-LiveActionV1-SPAN") == "2x-LiveActionV1-SPAN"
    with pytest.raises(converter.InstallError):
        converter.openmodeldb_id("../../etc/passwd")
    ranked = converter.ordered_resources([{"platform": "onnx"}, {"platform": "pytorch"}, {"platform": "tensorrt"},
                                          {"platform": "ncnn"}])
    assert [r["platform"] for r in ranked] == ["ncnn", "pytorch", "onnx"]


def test_private_model_addresses_are_refused(env, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_PRIVATE_MODEL_URLS", False)
    with pytest.raises(net.UnsafeURLError):
        net.validate_url("http://127.0.0.1/modele.pth")
    with pytest.raises(net.UnsafeURLError):
        net.validate_url("http://169.254.169.254/latest/meta-data")
    with pytest.raises(converter.InstallError):
        converter.download({}, "http://127.0.0.1:9/modele.pth", env / "telechargement")
    # Redirection vers une adresse privée : refusée avant d'être suivie.
    with pytest.raises(net.UnsafeURLError):
        net._CheckedRedirect(False).redirect_request(None, None, 302, "", {}, "http://10.0.0.1/modele.pth")
    monkeypatch.setattr(config, "ALLOW_PRIVATE_MODEL_URLS", True)
    assert net.validate_url("http://127.0.0.1/modele.pth", allow_private=True)


def test_drop_privileges_builds_setpriv_command(env, monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "modele.pth").write_text("x")
    monkeypatch.setattr(converter, "_nobody", lambda: (65534, 65534))
    monkeypatch.setattr(converter.shutil, "which", lambda name: "/usr/bin/setpriv")
    monkeypatch.setattr(converter.os, "chown", lambda *args, **kwargs: None)
    command = converter.drop_privileges(["python", "-I", "convert.py"], work)
    assert command == ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--",
                       "python", "-I", "convert.py"]


def test_drop_privileges_is_noop_when_not_root(env, monkeypatch):
    monkeypatch.setattr(converter.os, "geteuid", lambda: 1000, raising=False)
    command = ["python", "-I", "script.py"]
    assert converter.drop_privileges(command, env / "work") == command


def test_converter_environment_does_not_leak_secrets(env, monkeypatch):
    monkeypatch.setenv("UPSCALE_PASSWORD", "s3cret")
    values = converter.converter_env()
    assert "UPSCALE_PASSWORD" not in values
    assert values["PYTHONIOENCODING"] == "utf-8"


@needs_ffmpeg
def test_install_from_openmodeldb_link(env, fake_converter, http_server, monkeypatch):
    base, recorder = http_server
    monkeypatch.setattr(config, "OPENMODELDB_DATA", f"{base}/models")
    payload = b"PK-faux-pth" * 500
    recorder.responses["/models/2x-TestLive-SPAN.json"] = (200, json.dumps(omdb_entry(base, payload)).encode())
    recorder.responses["/files/2x_live.pth"] = (200, payload)
    client = app.test_client()
    response = client.post("/api/models/install", json={"source": "https://openmodeldb.info/models/2x-TestLive-SPAN"})
    assert response.status_code == 202, response.get_json()
    install_id = response.get_json()["install"]["id"]
    converter.process_pending()
    task = client.get(f"/api/models/installs/{install_id}").get_json()["install"]
    assert task["state"] == "done", task
    meta = models.get(task["model_id"])
    assert meta["label"] == "2xTestLive_SPAN" and meta["scale"] == 2 and meta["license"] == "CC-BY-NC-SA-4.0"
    assert meta["page"] == "https://openmodeldb.info/models/2x-TestLive-SPAN" and meta["architecture"] == "SPAN"
    assert meta["verified"]["psnr"] >= converter.MIN_PSNR  # moteur comparé au modèle d'origine
    param = (Path(meta["folder"]) / f"{models.NCNN_NAME}.param").read_text()
    assert "Input in0 0 1 data" in param and " output " in param  # noms attendus par le moteur
    assert any("dossier Google Drive" in line for line in task["log"])  # lien inutilisable ignoré
    listed = client.get("/api/models").get_json()
    assert listed["models"][meta["id"]]["license"] == "CC-BY-NC-SA-4.0"
    assert listed["installs"][0]["id"] == install_id and "_upload" not in listed["installs"][0]
    assert listed["suggestions"][0]["id"] == "2x-LiveActionV1-SPAN"
    ncnn = [c for c in calls(fake_converter) if c[0] == "ncnn"]
    assert ncnn and ncnn[-1][ncnn[-1].index("-z") + 1] == "2"
    st.save_settings({"model": meta["id"]})  # utilisable tout de suite
    assert not list(models.models_dir().glob(".installation-*"))  # dossier de travail nettoyé


@needs_ffmpeg
def test_install_rejects_tampered_or_unsupported_models(env, fake_converter, http_server, monkeypatch):
    base, recorder = http_server
    monkeypatch.setattr(config, "OPENMODELDB_DATA", f"{base}/models")
    payload = b"contenu" * 400
    entry = omdb_entry(base, payload)
    entry["resources"][0]["sha256"] = "0" * 64
    recorder.responses["/models/2x-Modifie.json"] = (200, json.dumps(entry).encode())
    recorder.responses["/files/2x_live.pth"] = (200, payload)
    recorder.responses["/models/8x-Enorme.json"] = (200, json.dumps({**omdb_entry(base, payload), "scale": 8}).encode())
    recorder.responses["/models/Absent.json"] = (404, b"{}")
    client = app.test_client()
    ids = [client.post("/api/models/install", json={"source": name}).get_json()["install"]["id"]
           for name in ("2x-Modifie", "8x-Enorme", "Absent")]
    converter.process_pending()
    tampered, huge, missing = (converter.get_install(i) for i in ids)
    assert tampered["state"] == "error" and "SHA-256" in tampered["message"]
    assert huge["state"] == "error" and "×8" in huge["message"]
    assert missing["state"] == "error" and "introuvable" in missing["message"]
    assert client.post("/api/models/install", json={"source": "pas un lien/../x"}).status_code == 400
    assert not models.list_models()


@needs_ffmpeg
def test_upload_pth_onnx_and_ncnn_zip(env, fake_converter, http_server):
    client = app.test_client()
    upload = lambda name, data, **form: client.post(  # noqa: E731
        "/api/models", headers={"X-Upscale-Upload": "1"}, content_type="multipart/form-data",
        data={"tile": "0", **form, "file": (io.BytesIO(data), name)})
    pth = upload("4x_Peau.pth", b"x" * 5000, label="Peau ×4")
    onnx_no_scale = upload("modele.onnx", b"x" * 5000)
    onnx = upload("modele.onnx", b"x" * 5000, scale="3", label="Onnx x3")
    gelu = upload("2x_gelu.pth", b"x" * 5000)
    dat = upload("4x_DAT.pth", b"x" * 5000)
    assert upload("notes.txt", b"x").status_code == 400
    ids = [r.get_json()["install"]["id"] for r in (pth, onnx_no_scale, onnx, gelu, dat)]
    assert all(r.status_code == 202 for r in (pth, onnx_no_scale, onnx, gelu, dat))
    converter.process_pending()
    done_pth, no_scale, done_onnx, failed_gelu, failed_dat = (converter.get_install(i) for i in ids)
    assert done_pth["state"] == "done" and models.get(done_pth["model_id"])["scale"] == 4
    assert models.get(done_pth["model_id"])["label"] == "Peau ×4"
    assert no_scale["state"] == "error" and "facteur" in no_scale["message"]
    assert done_onnx["state"] == "done" and models.get(done_onnx["model_id"])["scale"] == 3
    assert models.get(done_onnx["model_id"])["verified"].get("psnr") is None  # pas de référence PyTorch
    assert failed_gelu["state"] == "error" and "Gelu" in failed_gelu["message"]  # couche inconnue du moteur
    assert failed_dat["state"] == "error" and "DAT" in failed_dat["message"]
    assert not list(models.models_dir().glob(".envoi-*"))  # fichiers envoyés supprimés

    # Archive ncnn prête à l'emploi (lien direct) : aucune conversion.
    base, recorder = http_server
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("modele/x2.param", PNNX_PARAM)
        z.writestr("modele/x2.bin", b"\0" * 4096)
        z.writestr("../../evasion.param", "ignoré : un seul .param attendu")
    recorder.responses["/ncnn.zip"] = (200, archive.getvalue())
    install = client.post("/api/models/install", json={"source": f"{base}/ncnn.zip", "scale": 2}).get_json()["install"]
    converter.process_pending()
    assert converter.get_install(install["id"])["state"] == "error"  # deux .param : ambigu
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("modele/x2.param", PNNX_PARAM)
        z.writestr("modele/x2.bin", b"\0" * 4096)
    recorder.responses["/ncnn.zip"] = (200, archive.getvalue())
    before = len([c for c in calls(fake_converter) if c[0] == "converter"])
    install = client.post("/api/models/install", json={"source": f"{base}/ncnn.zip", "scale": 2,
                                                       "label": "Zip ncnn"}).get_json()["install"]
    converter.process_pending()
    task = converter.get_install(install["id"])
    assert task["state"] == "done", task
    assert len([c for c in calls(fake_converter) if c[0] == "converter"]) == before
    assert not (env.parent / "evasion.param").exists()


def test_converter_environment_is_installed_once(env, monkeypatch):
    monkeypatch.setattr(config, "CONVERTER_PYTHON", "")
    commands = []

    def fake_stream(task, command, **kwargs):
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            python = Path(command[3]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\nexit 0\n")
            python.chmod(0o755)
        return 0, []

    monkeypatch.setattr(converter, "stream", fake_stream)
    task = {"id": "t", "log": []}
    python = converter.ensure_converter(task)
    assert python == config.converter_dir() / "bin" / "python"
    pip_torch = next(c for c in commands if "--index-url" in c)
    assert pip_torch[pip_torch.index("--index-url") + 1] == config.TORCH_INDEX
    assert any(p.startswith("torch==") for p in pip_torch)
    assert any(any(p.startswith("spandrel==") for p in c) and any(p.startswith("pnnx==") for p in c) for c in commands)
    commands.clear()
    assert converter.ensure_converter(task) == python and not commands  # déjà prêt : rien à installer
    assert converter.converter_status()["installed"] is True


def test_native_esrgan_writer_matches_pytorch(tmp_path):
    """Écriture ncnn directe d'ESRGAN, comparée à PyTorch (si torch, spandrel et ncnn sont installés)."""
    torch = pytest.importorskip("torch")
    ncnn = pytest.importorskip("ncnn")
    rrdb = pytest.importorskip("spandrel.architectures.ESRGAN.__arch.RRDB")
    import importlib.util

    import numpy as np
    spec = importlib.util.spec_from_file_location("convert_model", Path(converter.CONVERT_SCRIPT))
    convert_model = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(convert_model)
    torch.manual_seed(0)
    for scale, plus in ((2, False), (4, True)):
        net = rrdb.RRDBNet(num_filters=16, num_blocks=2, scale=scale, plus=plus).eval()
        descriptor = type("D", (), {"model": net, "architecture": type("A", (), {"name": "ESRGAN"})})
        work = tmp_path / f"x{scale}"
        work.mkdir()
        convert_model.write_native(descriptor, work)
        models.normalize_param((work / "model.param").read_text())  # format accepté à l'import
        sample = torch.rand(1, 3, 20, 26)
        with torch.no_grad():
            expected = net(sample)[0].numpy()
        engine = ncnn.Net()
        engine.opt.use_fp16_storage = engine.opt.use_fp16_packed = engine.opt.use_fp16_arithmetic = False
        engine.load_param(str(work / "model.param"))
        engine.load_model(str(work / "model.bin"))
        extractor = engine.create_extractor()
        pixels = np.ascontiguousarray(sample[0].numpy())  # référence gardée : ncnn.Mat ne copie pas
        extractor.input("data", ncnn.Mat(pixels))
        _, out = extractor.extract("output")
        assert np.abs(np.array(out) - expected).max() < 1e-4
