"""File d'attente, planification et maintenance."""
from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest
from conftest import make_video, needs_ffmpeg
from helpers import (
    insert_job,
)

from upscale import config, db, pipeline, runner, scanner, worker
from upscale import settings as st
from upscale.web import app


def test_within_schedule():
    cfg = {"schedule_enabled": True, "schedule_start": "22:00", "schedule_end": "07:00"}
    assert worker.within_schedule(cfg, datetime(2026, 1, 1, 23, 30))
    assert worker.within_schedule(cfg, datetime(2026, 1, 1, 6, 59))
    assert not worker.within_schedule(cfg, datetime(2026, 1, 1, 12, 0))
    day = {**cfg, "schedule_start": "09:00", "schedule_end": "17:00"}
    assert worker.within_schedule(day, datetime(2026, 1, 1, 12, 0))
    assert not worker.within_schedule(day, datetime(2026, 1, 1, 18, 0))
    assert worker.within_schedule({**cfg, "schedule_enabled": False}, datetime(2026, 1, 1, 12, 0))


@needs_ffmpeg
def test_enqueue_freezes_settings(env):
    make_video(env / "videos" / "anime" / "a.mp4")
    st.save_settings({"profiles": [{"path": str(env / "videos" / "anime"), "model": "realesr-animevideov3"}]})
    settings = st.get_settings()
    files = scanner.scan_files(settings)["files"]
    assert scanner.enqueue(files, settings)["added"] == 1
    st.save_settings({"output_height": 1080, "profiles": []})
    job = worker.claim_next_job()
    frozen = pipeline.job_settings(job)
    assert frozen["model"] == "realesr-animevideov3" and frozen["output_height"] == 720
    again = scanner.scan_files(st.get_settings())
    assert again["files"] == [] and again["already_known"] == 1
    other = {**files[0], "source": str(env / "videos" / "b.mp4"), "mtime_ns": 1}
    assert scanner.enqueue([other], st.get_settings()) == {"added": 0, "conflicts": 1}


def test_cancel_before_claim_wins(env):
    insert_job("aaaaaaaaaaaa")
    assert app.test_client().post("/api/jobs/aaaaaaaaaaaa/cancel", json={}).status_code == 200
    assert worker.claim_next_job() is None
    assert db.get_job("aaaaaaaaaaaa")["status"] == "cancelled"


def test_jobs_all_includes_previews(env):
    insert_job("aaaaaaaaaaa1", status="queued", position=1.0)
    insert_job("bbbbbbbbbbb1", status="queued", position=2.0, kind="preview", priority=100)
    data = app.test_client().get("/api/jobs?kind=all").get_json()
    assert {job["id"] for job in data["jobs"]} == {"aaaaaaaaaaa1", "bbbbbbbbbbb1"}
    assert data["preview_counts"]["queued"] == 1
    assert [job["id"] for job in data["jobs"]][0] == "bbbbbbbbbbb1"  # aperçu prioritaire
    previews = app.test_client().get("/api/jobs?kind=preview").get_json()["jobs"]
    assert [job["id"] for job in previews] == ["bbbbbbbbbbb1"]


def test_queue_management(env):
    for index, job_id in enumerate(["aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3"]):
        insert_job(job_id, position=float(index))
    client = app.test_client()
    assert client.post("/api/jobs/aaaaaaaaaaa3/move", json={"to": "top"}).status_code == 200
    assert client.post("/api/jobs/aaaaaaaaaaa1/move", json={"to": "down"}).status_code == 200
    order = [j["id"] for j in client.get("/api/jobs?status=queued").get_json()["jobs"]]
    assert order == ["aaaaaaaaaaa3", "aaaaaaaaaaa2", "aaaaaaaaaaa1"]
    assert worker.claim_next_job()["id"] == "aaaaaaaaaaa3"

    db.set_job("aaaaaaaaaaa3", status="failed")
    assert client.post("/api/jobs/aaaaaaaaaaa3/retry", json={}).status_code == 200
    assert db.get_job("aaaaaaaaaaa3")["status"] == "queued"
    assert client.post("/api/jobs/aaaaaaaaaaa2/retry", json={}).status_code == 400

    db.set_job("aaaaaaaaaaa1", status="completed")
    assert client.post("/api/jobs/clear", json={"statuses": ["completed"]}).get_json()["deleted"] == 1
    assert client.post("/api/jobs/aaaaaaaaaaa2/delete", json={}).status_code == 200
    data = client.get("/api/jobs?limit=1&page=1").get_json()
    assert data["total"] == 1 and len(data["jobs"]) == 1


def test_move_to_index(env):
    for index, job_id in enumerate(["aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3"]):
        insert_job(job_id, position=float(index))
    client = app.test_client()
    assert client.post("/api/jobs/aaaaaaaaaaa3/move", json={"to": "index", "index": 0}).status_code == 200
    assert [job["id"] for job in client.get("/api/jobs?status=queued").get_json()["jobs"]] == \
        ["aaaaaaaaaaa3", "aaaaaaaaaaa1", "aaaaaaaaaaa2"]
    assert client.post("/api/jobs/aaaaaaaaaaa1/move", json={"to": "index", "index": 2}).status_code == 200
    assert [job["id"] for job in client.get("/api/jobs?status=queued").get_json()["jobs"]] == \
        ["aaaaaaaaaaa3", "aaaaaaaaaaa2", "aaaaaaaaaaa1"]
    assert client.post("/api/jobs/aaaaaaaaaaa1/move", json={"to": "index", "index": "x"}).status_code == 400


def test_move_relative_before_and_after(env):
    for index, job_id in enumerate(["aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3"]):
        insert_job(job_id, position=float(index))
    client = app.test_client()
    assert client.post("/api/jobs/aaaaaaaaaaa3/move",
                       json={"to": "before", "id": "aaaaaaaaaaa1"}).status_code == 200
    assert [job["id"] for job in client.get("/api/jobs?status=queued").get_json()["jobs"]] == \
        ["aaaaaaaaaaa3", "aaaaaaaaaaa1", "aaaaaaaaaaa2"]
    assert client.post("/api/jobs/aaaaaaaaaaa3/move",
                       json={"to": "after", "id": "aaaaaaaaaaa2"}).status_code == 200
    assert [job["id"] for job in client.get("/api/jobs?status=queued").get_json()["jobs"]] == \
        ["aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3"]
    assert client.post("/api/jobs/aaaaaaaaaaa1/move",
                       json={"to": "before", "id": "ffffffffffff"}).status_code == 400


def test_previews_jump_the_queue_and_ignore_pause(env):
    insert_job("aaaaaaaaaaa1", position=1.0)
    insert_job("bbbbbbbbbbb1", position=2.0, kind="preview", priority=100)
    db.set_paused(True)
    assert worker.claim_next_job()["id"] == "bbbbbbbbbbb1"
    runner.set_active(None)
    assert worker.claim_next_job() is None


def test_watch_tick(env, monkeypatch):
    started = []
    monkeypatch.setattr(scanner, "start_scan", lambda *a, **k: started.append(k) or True)
    assert worker.watch_tick(1000.0) is False
    st.save_settings({"watch_enabled": True, "watch_interval": 10})
    assert worker.watch_tick(1000.0) is True and started[0]["min_age_seconds"] == 120
    assert worker.watch_tick(1000.0 + 300) is False
    assert worker.watch_tick(1000.0 + 601) is True


def test_maintenance(env, monkeypatch):
    monkeypatch.setattr(config, "PREVIEW_KEEP", 1)
    (config.log_dir() / "ffffffffffff.log").write_text("orphelin")
    (config.thumbnail_dir() / "orphelin.jpg").write_bytes(b"x")
    with db.connect() as conn:
        conn.execute("INSERT INTO probe_cache(path,size,mtime_ns,width,height,duration,updated_at) "
                     "VALUES('/nexiste/pas.mkv',1,1,10,10,NULL,?)", (db.now(),))
    for index in range(3):
        job_id = f"ccccccccccc{index}"
        insert_job(job_id, status="completed", kind="preview")
        (config.preview_dir() / job_id).mkdir(parents=True)
        db.set_job(job_id, updated_at=f"2026-01-0{index + 1}T00:00:00+00:00")
    result = worker.maintenance()
    assert result == {"logs": 1, "probe_cache": 1, "previews": 2, "thumbnails": 1,
                      "owners": 0, "trash": 0}
    assert sorted(p.name for p in config.preview_dir().iterdir()) == ["ccccccccccc2"]


def test_run_process_cancellation(env):
    job_id = "bbbbbbbbbbbb"
    runner.set_active(job_id)
    threading.Timer(0.5, runner.request_cancel, args=(job_id,)).start()
    started = time.monotonic()
    with pytest.raises(runner.JobCancelled):
        runner.run_process(job_id, ["sleep", "30"])
    assert time.monotonic() - started < 10


def test_cancel_finished_job_is_conflict(env):
    with db.connect() as conn:
        conn.execute("""INSERT INTO jobs (id,source,destination,source_size,source_mtime_ns,status,stage,
                                          created_at,updated_at)
                        VALUES('bbbbbbbbbbbb','/data/a.mkv','/data/out/a.mkv',1,1,'completed','Terminé',
                               '2026-01-01','2026-01-01')""")
    client = app.test_client()
    assert client.post("/api/jobs/bbbbbbbbbbbb/cancel", json={}).status_code == 409
    assert client.post("/api/jobs/cccccccccccc/cancel", json={}).status_code == 404
