"""API HTTP, authentification et intégrations."""
from __future__ import annotations

import base64
import json
import time

from upscale import config, db, gpu, notify, updates
from upscale import settings as st
from upscale import web as web_module
from upscale.web import app


def test_post_requires_json_and_same_origin(env, monkeypatch):
    client = app.test_client()
    assert client.post("/api/pause", data="{}", content_type="text/plain").status_code == 415
    assert client.post("/api/pause", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/pause", json={}, headers={"Origin": "http://localhost"}).status_code == 200
    # DNS rebinding : Host et Origin identiques, mais nom non autorisé.
    rebinding = {"Origin": "http://evil.example:27000"}
    assert client.post("/api/pause", json={}, base_url="http://evil.example:27000",
                       headers=rebinding).status_code == 403
    # Accès direct par IP (LAN) : autorisé.
    assert client.post("/api/pause", json={}, base_url="http://192.168.1.10:27000",
                       headers={"Origin": "http://192.168.1.10:27000"}).status_code == 200
    # Reverse proxy : le nom public doit figurer dans UPSCALE_TRUSTED_HOSTS.
    monkeypatch.setattr(config, "TRUSTED_HOSTS", {"upscale.maison.lan"})
    proxied = {"Origin": "https://upscale.maison.lan"}
    assert client.post("/api/pause", json={}, base_url="https://upscale.maison.lan",
                       headers=proxied).status_code == 200
    assert client.post("/api/pause", json={}, base_url="https://upscale.maison.lan",
                       headers={"Origin": "https://evil.example"}).status_code == 403


def test_basic_auth(env, monkeypatch):
    monkeypatch.setattr(config, "AUTH_PASSWORD", "secret")
    client = app.test_client()
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/ping").status_code == 200
    token = base64.b64encode(b"admin:secret").decode()
    assert client.get("/api/jobs", headers={"Authorization": f"Basic {token}"}).status_code == 200


def test_bad_ids_rejected(env):
    client = app.test_client()
    assert client.get("/api/logs/..%2f..%2fetc").status_code in {400, 404}
    assert client.get("/api/previews/aaaaaaaaaaaa/..%2fx.mp4").status_code == 404


def test_anonymous_mode_blocks_mutations(env, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_ANONYMOUS", False)
    client = app.test_client()
    assert client.get("/api/config").status_code == 200
    response = client.post("/api/pause", json={})
    assert response.status_code == 403
    assert "UPSCALE_PASSWORD" in response.get_json()["error"]
    monkeypatch.setattr(config, "ALLOW_ANONYMOUS", True)
    assert client.post("/api/pause", json={}).status_code == 200


def test_config_reports_auth_state(env, monkeypatch):
    monkeypatch.setattr(config, "AUTH_PASSWORD", "")
    monkeypatch.setattr(config, "ALLOW_ANONYMOUS", False)
    data = app.test_client().get("/api/config").get_json()
    assert data["auth"] == {"password_set": False, "anonymous_allowed": False, "env_forced": False}


def test_lan_mode_can_be_enabled_from_ui(env, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_ANONYMOUS", False)
    web_module._lan_cache.update(value=None, at=0.0)
    client = app.test_client()
    assert client.post("/api/pause", json={}).status_code == 403
    # Le réglage est protégé par la même vérification d'origine que le reste de l'API.
    assert client.post("/api/auth/lan-mode", json={"enabled": True},
                       headers={"Origin": "http://evil.example"}).status_code == 403
    response = client.post("/api/auth/lan-mode", json={"enabled": True})
    assert response.status_code == 200
    assert response.get_json()["auth"]["anonymous_allowed"] is True
    assert client.post("/api/pause", json={}).status_code == 200
    assert db.get_state("lan_mode") == "1"
    assert client.post("/api/auth/lan-mode", json={"enabled": False}).status_code == 200
    assert client.post("/api/pause", json={}).status_code == 403


def test_lan_mode_is_disabled_by_password_or_env(env, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_ANONYMOUS", True)
    assert app.test_client().post("/api/auth/lan-mode", json={"enabled": False}).status_code == 409
    monkeypatch.setattr(config, "ALLOW_ANONYMOUS", False)
    monkeypatch.setattr(config, "AUTH_PASSWORD", "secret")
    token = base64.b64encode(b"admin:secret").decode()
    response = app.test_client().post("/api/auth/lan-mode", json={"enabled": True},
                                      headers={"Authorization": f"Basic {token}"})
    assert response.status_code == 409


def test_update_check_reads_github_release(env, http_server, monkeypatch):
    base, recorder = http_server
    recorder.responses["/latest"] = (200, json.dumps({
        "tag_name": "v9.9.9", "html_url": "https://example.test/release", "body": "notes"}).encode())
    monkeypatch.setattr(updates, "API_URL", f"{base}/latest")
    updates.reset_cache()
    data = updates.state(force=True)
    assert data["latest"] == "9.9.9" and data["update_available"] is True
    assert data["url"] == "https://example.test/release" and data["notes"] == "notes"
    assert data["error"] is None
    assert app.test_client().get("/api/update?refresh=1").get_json()["latest"] == "9.9.9"


def test_update_check_reports_404_and_uses_cache(env, http_server, monkeypatch):
    base, recorder = http_server
    recorder.responses["/latest"] = (404, b"{}")
    monkeypatch.setattr(updates, "API_URL", f"{base}/latest")
    updates.reset_cache()
    first = updates.state(force=True)
    assert "Aucune release" in first["error"]
    recorder.responses["/latest"] = (200, json.dumps({"tag_name": "v9.9.9"}).encode())
    assert updates.state() == first  # cache de 30 minutes
    assert updates.state(force=True)["latest"] == "9.9.9"


def test_update_version_parsing():
    assert updates.parse_version("v2.10.0") == (2, 10, 0)
    assert updates.parse_version("2.9.1") == (2, 9, 1)
    assert updates.parse_version("dev") is None


def test_security_headers(env):
    client = app.test_client()
    response = client.get("/api/ping")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert client.get("/").headers["Cache-Control"] == "no-cache"
    assert client.get("/static/app.js").headers["Cache-Control"] == "no-cache"


def test_auth_failures_are_throttled(env, monkeypatch):
    monkeypatch.setattr(config, "AUTH_PASSWORD", "secret")
    client = app.test_client()
    for _ in range(web_module.AUTH_MAX_FAILURES):
        assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs").status_code == 429


def test_gpu_check_is_async_and_cached(env, fake_docker):
    client = app.test_client()
    assert client.post("/api/health/gpu", json={}).status_code == 202
    for _ in range(50):
        state = client.get("/api/health").get_json()["gpu"]
        if state["status"] != "running":
            break
        time.sleep(0.1)
    assert state["status"] == "ok" and "RX 5500 XT" in state["devices"][0]


def test_gpu_stats_from_sysfs(env, tmp_path, monkeypatch):
    drm = tmp_path / "drm"
    device = tmp_path / "pci" / "0000:03:00.0"
    (device / "hwmon" / "hwmon3").mkdir(parents=True)
    (tmp_path / "drivers" / "amdgpu").mkdir(parents=True)
    (device / "driver").symlink_to(tmp_path / "drivers" / "amdgpu")
    for name, value in {"gpu_busy_percent": "87", "mem_info_vram_used": str(2 * 1024**3),
                        "mem_info_vram_total": str(4 * 1024**3)}.items():
        (device / name).write_text(value)
    for name, value in {"temp1_input": "63000", "power1_average": "95000000", "fan1_input": "1500"}.items():
        (device / "hwmon" / "hwmon3" / name).write_text(value)
    (drm / "card1").mkdir(parents=True)
    (drm / "card1" / "device").symlink_to(device)
    (drm / "card1-HDMI-A-1").mkdir()
    monkeypatch.setattr(config, "SYSFS_DRM", drm)
    [card] = app.test_client().get("/api/gpu/stats").get_json()["cards"]
    assert card == {"card": "card1", "driver": "amdgpu", "busy_percent": 87, "vram_used": 2 * 1024**3, "vram_total": 4 * 1024**3,
                    "temperature_c": 63.0, "power_w": 95.0, "fan_rpm": 1500}
    assert gpu.stats()[0]["busy_percent"] == 87
    # Carte Intel (Arc) : pas de charge ni de VRAM dans sysfs, mais la carte est listée.
    intel = tmp_path / "pci" / "0000:04:00.0"
    intel.mkdir(parents=True)
    (tmp_path / "drivers" / "xe").mkdir()
    (intel / "driver").symlink_to(tmp_path / "drivers" / "xe")
    (drm / "card2").mkdir()
    (drm / "card2" / "device").symlink_to(intel)
    arc = gpu.stats()[1]
    assert arc["driver"] == "xe" and arc["busy_percent"] is None and arc["vram_total"] is None


def test_scan_endpoint_runs_in_background(env):
    client = app.test_client()
    settings = st.get_settings()
    assert client.post("/api/scan", json={"source": settings["source"],
                                          "destination": settings["destination"]}).status_code == 202
    for _ in range(50):
        state = client.get("/api/scan").get_json()
        if not state["running"]:
            break
        time.sleep(0.1)
    assert state["found"] == 0 and state["error"] is None


def test_ntfy_gotify_webhook(env, http_server):
    base, recorder = http_server
    notify.send({"notify_type": "ntfy", "notify_url": f"{base}/upscale", "notify_token": "tk"},
                "failed", "Upscale : échec", "film.mkv")
    ntfy = recorder.received[-1]
    assert ntfy["path"].startswith("/upscale?") and "title=Upscale" in ntfy["path"] and "priority=high" in ntfy["path"]
    assert ntfy["headers"]["Authorization"] == "Bearer tk" and ntfy["body"] == b"film.mkv"
    notify.send({"notify_type": "gotify", "notify_url": base, "notify_token": "gk"}, "completed", "T", "M")
    gotify = recorder.received[-1]
    assert gotify["path"] == "/message" and gotify["headers"]["X-Gotify-Key"] == "gk"
    assert json.loads(gotify["body"])["title"] == "T"
    notify.send({"notify_type": "webhook", "notify_url": f"{base}/hook"}, "completed", "T", "M", {"job_id": "x"})
    assert json.loads(recorder.received[-1]["body"]) == {"event": "completed", "title": "T", "message": "M", "job_id": "x"}

    st.save_settings({"notify_type": "webhook", "notify_url": f"{base}/hook"})
    assert app.test_client().post("/api/notify/test", json={}).status_code == 200


def test_media_server_refresh(env, http_server):
    base, recorder = http_server
    assert "Jellyfin" in notify.refresh_media_server({"media_type": "jellyfin", "media_url": base, "media_token": "j"})
    assert recorder.received[-1]["path"] == "/Library/Refresh"
    assert recorder.received[-1]["headers"]["X-Emby-Token"] == "j"
    assert "Emby" in notify.refresh_media_server({"media_type": "emby", "media_url": base, "media_token": "e"})
    assert recorder.received[-1]["path"] == "/Library/Refresh"
    assert recorder.received[-1]["headers"]["X-Emby-Token"] == "e"
    recorder.responses["/library/sections"] = (200, json.dumps(
        {"MediaContainer": {"Directory": [{"key": "1"}, {"key": "4"}]}}).encode())
    message = notify.refresh_media_server({"media_type": "plex", "media_url": base, "media_token": "p"})
    assert "2 bibliothèque" in message
    paths = [r["path"] for r in recorder.received[-2:]]
    assert paths == ["/library/sections/1/refresh?X-Plex-Token=p", "/library/sections/4/refresh?X-Plex-Token=p"]
