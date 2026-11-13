"""Analyse de la bibliothèque, catalogue et disques."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from conftest import make_video, needs_ffmpeg
from helpers import (
    add_disk,
)

from upscale import config, db, media, scanner
from upscale import settings as st
from upscale.web import app


@needs_ffmpeg
def test_library_catalog_from_scan_and_selection(env):
    source = make_video(env / "videos" / "film.mkv", width=320, height=240)
    settings = st.get_settings()
    scanner._run_scan(settings, False, 0)
    page = db.library_page(1, 20)
    assert page["counts"]["total"] == 1 and page["counts"]["new"] == 1
    [item] = page["items"]
    assert item["source"] == str(source) and (item["width"], item["height"]) == (320, 240)
    assert item["duration"] and 0.9 <= item["duration"] <= 1.5
    client = app.test_client()
    data = client.get("/api/library").get_json()
    assert data["total"] == 1 and data["items"][0]["thumbnail"].endswith("/thumbnail")
    thumb = client.get(data["items"][0]["thumbnail"])
    assert thumb.status_code == 200 and thumb.mimetype == "image/jpeg"

    result = client.post("/api/library/queue", json={"ids": [item["id"]]})
    assert result.status_code == 200 and result.get_json()["added"] == 1
    queued = db.library_page(1, 20, status="queued")
    assert queued["total"] == 1 and queued["items"][0]["job_status"] == "queued"
    assert client.get("/api/library?filter=done").get_json()["total"] == 0


def test_probe_dimensions_ignores_attached_cover(monkeypatch):
    monkeypatch.setattr(media, "ffprobe_json", lambda *a, **k: {"streams": [
        {"codec_name": "mjpeg", "width": 200, "height": 150, "disposition": {"attached_pic": 1}},
        {"codec_name": "h264", "width": 1920, "height": 1080, "disposition": {}},
    ]})
    assert media.probe_dimensions(Path("/data/film.mkv")) == (1920, 1080)


def test_probe_metadata_returns_duration(monkeypatch):
    monkeypatch.setattr(media, "ffprobe_json", lambda *a, **k: {
        "streams": [{"codec_name": "h264", "width": 1280, "height": 720, "disposition": {}}],
        "format": {"duration": "95.5"},
    })
    assert media.probe_metadata(Path("/data/film.mkv")) == (1280, 720, 95.5)


def test_probe_cache_is_invalidated_when_version_changes(env):
    with db.connect() as conn:
        conn.execute("INSERT INTO probe_cache(path,size,mtime_ns,width,height,duration,updated_at) "
                     "VALUES('/data/vieux.mkv',1,1,320,180,NULL,?)", (db.now(),))
    db.set_state("probe_cache_version", "1")
    db.init_db()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM probe_cache").fetchone()[0] == 0
    assert db.get_state("probe_cache_version") == str(config.PROBE_CACHE_VERSION)


@needs_ffmpeg
def test_scan_reports_resolution_histogram(env):
    videos = env / "videos"
    make_video(videos / "sd.mkv", width=160, height=120)
    make_video(videos / "dvd.mkv", width=640, height=480)
    make_video(videos / "hd.mkv", width=1280, height=720)
    result = scanner.scan_files(st.get_settings())
    assert result["resolutions"] == {"under_360": 1, "360_480": 1, "over_480": 1, "unknown": 0}
    assert len(result["files"]) == 2  # 120p et 480p sont sous la limite de 480p


@needs_ffmpeg
def test_excluded_folders_validation_and_scan(env):
    videos = env / "videos"
    extras = videos / "Extras"
    extras.mkdir(parents=True)
    nested = extras / "Sous"
    nested.mkdir()
    (env / "out").mkdir()
    saved = st.save_settings({"exclude_dirs": [str(extras), str(nested)]})
    assert saved["exclude_dirs"] == [str(extras.resolve())]
    with pytest.raises(ValueError, match="à l'intérieur"):
        st.save_settings({"exclude_dirs": [str(env / "out")]})
    with pytest.raises(ValueError, match="à l'intérieur"):
        st.save_settings({"exclude_dirs": [str(videos)]})
    make_video(videos / "film.mkv", width=160, height=120)
    make_video(extras / "bonus.mkv", width=160, height=120)
    result = scanner.scan_files(st.get_settings())
    assert [Path(item["source"]).name for item in result["files"]] == ["film.mkv"]


@needs_ffmpeg
def test_library_duration_filters_sort_and_ids(env):
    make_video(env / "videos" / "court.mkv", seconds=1)
    make_video(env / "videos" / "long.mkv", seconds=3)
    scanner._run_scan(st.get_settings(), False, 0)
    client = app.test_client()
    filtered = client.get("/api/library?min_duration=2").get_json()
    assert filtered["total"] == 1 and filtered["items"][0]["duration"] >= 2
    ordered = client.get("/api/library?sort=duration_desc").get_json()["items"]
    durations = [item["duration"] for item in ordered]
    assert durations == sorted(durations, reverse=True)
    ids = client.get("/api/library/ids?min_duration=2").get_json()["ids"]
    assert ids == [filtered["items"][0]["id"]]


def test_scan_disambiguates_collisions_and_uses_cache(env, monkeypatch):
    videos = env / "videos"
    make_video(videos / "film.mp4")
    make_video(videos / "film.avi", extra=["-f", "avi"])
    make_video(videos / "autre.mkv")
    make_video(videos / "grand.mp4", width=1280, height=720)
    result = scanner.scan_files(st.get_settings())
    assert sorted(Path(f["destination"]).name for f in result["files"]) == ["autre.mkv", "film.avi.mkv", "film.mp4.mkv"]
    probed = []
    monkeypatch.setattr(media, "probe_metadata", lambda path: probed.append(path))
    assert len(scanner.scan_files(st.get_settings())["files"]) == 3 and probed == []


@needs_ffmpeg
def test_scan_parent_with_subfolders_and_destination_inside(env):
    videos = env / "videos"
    make_video(videos / "Films" / "film.mkv", width=160, height=120)
    make_video(videos / "Series" / "episode.mkv", width=160, height=120)
    make_video(videos / "out" / "deja_traite.mkv", width=160, height=120)
    settings = st.get_settings()
    settings.update({"sources": [str(videos)], "source": str(videos),
                     "destination": str(videos / "out"), "include_subfolders": True})
    result = scanner.scan_files(settings)
    names = sorted(Path(item["source"]).name for item in result["files"])
    assert names == ["episode.mkv", "film.mkv"]


@needs_ffmpeg
def test_scan_non_recursive_ignores_subfolders(env):
    videos = env / "videos"
    make_video(videos / "racine.mkv", width=160, height=120)
    make_video(videos / "Sous" / "enfant.mkv", width=160, height=120)
    settings = st.get_settings()
    settings.update({"sources": [str(videos)], "source": str(videos),
                     "destination": str(env / "out"), "include_subfolders": False})
    assert [Path(item["source"]).name for item in scanner.scan_files(settings)["files"]] == ["racine.mkv"]


def test_save_dedupes_nested_sources(env):
    videos = env / "videos"
    (videos / "Films").mkdir()
    saved = st.save_settings({"sources": [str(videos / "Films"), str(videos)],
                              "destination": str(env / "out")})
    assert saved["sources"] == [str(videos.resolve())]


def test_scan_skips_files_being_copied(env):
    path = make_video(env / "videos" / "copie.mp4")
    assert scanner.scan_files(st.get_settings(), min_age_seconds=120)["too_recent"] == 1
    old = time.time() - 600
    os.utime(path, (old, old))
    assert len(scanner.scan_files(st.get_settings(), min_age_seconds=120)["files"]) == 1


def test_disks_are_browsable_roots(env):
    disk = add_disk("Disque2")
    (disk / "Series").mkdir()
    assert config.roots() == [config.DATA_ROOT, disk]
    assert st.safe_path(str(disk / "Series"), must_exist=True) == disk / "Series"
    with pytest.raises(ValueError, match="hors des dossiers accessibles"):
        st.safe_path(str(config.DISKS_ROOT.parent))
    client = app.test_client()
    home = client.get("/api/browse").get_json()
    assert [d["path"] for d in home["disks"]] == [str(config.DATA_ROOT), str(disk)]
    inside = client.get("/api/browse", query_string={"path": str(disk)}).get_json()
    assert inside["parent"] == "" and [d["name"] for d in inside["directories"]] == ["Series"]
    with pytest.raises(ValueError, match="sous-dossier d'un disque"):
        st.validate_folders({**st.get_settings(), "destination": str(disk)})


def test_legacy_single_source_settings(env):
    payload = {k: v for k, v in st.get_settings().items() if k != "sources"}
    payload["source"] = str(env / "videos")
    db.store_settings_payload(payload)
    assert st.get_settings()["sources"] == [str(env / "videos")]


def test_source_labels_are_unique():
    labels = scanner.source_labels([Path("/data/Films"), Path("/disques/B/Films"), Path("/disques/C/Series")])
    assert list(labels.values()) == ["Films", "Films-2", "Series"]


def test_scan_ignores_upscale_internal_folders(env):
    for folder in (".upscale-abc123def456/src", ".upscale-corbeille", ".uscale-old"):
        (env / "videos" / folder).mkdir(parents=True)
        (env / "videos" / folder / "seg.mkv").write_bytes(b"x")
    (env / "videos" / "vrai.mkv").write_bytes(b"x")
    names = [p.name for p in scanner.list_candidates(env / "videos", True)]
    assert names == ["vrai.mkv"]
