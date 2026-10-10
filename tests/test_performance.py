import asyncio
import math
import sqlite3
import uuid

import pytest
from test_rooms import join, send, setup

from pocket.core import Store
from pocket.performance import Reply, measured
from pocket.server import make_app


def test_usage_counts_only_output_and_no_guess():
    for envelope in [
        {"usage": {"output": 20, "input": 500, "total": 520}},
        {"result": {"meta": {"agentMeta": {"usage": {"output": 20}}}}},
    ]:
        assert measured(Reply("hello", envelope), 4)["output_tokens"] == 20
    for count in [None, True, -1, 1.5, "20", math.nan]:
        assert (
            measured(Reply("long text", {"usage": {"output": count}}), 4)["output_tokens"] is None
        )
    assert measured("plain old runner", 4)["output_tokens"] is None
    assert measured(Reply("", {"usage": {"output": 0}}), 2)["output_tokens"] == 0
    for seconds in [0, -1, math.inf, math.nan]:
        assert measured("text", seconds) is None


def test_snapshot_switch_retries_and_persistence(tmp_path):
    path = tmp_path / "db"
    s = Store(path)
    a = s.pair(s.invite()[0], "owner")["device_id"]
    b = s.pair(s.invite()[0], "other")["device_id"]
    c = s.create_chat(a, "first")["id"]
    jid = str(uuid.uuid4())
    s.submit(a, c, jid, "one")
    s.update_chat(a, c, {"model": "second"})
    assert s.submit(a, c, jid, "one")["model_snapshot"] == "first"
    assert s.claim()["model"] == "first"
    s.update_chat(a, c, {"model": "third"})
    with pytest.raises(KeyError):
        s.update_chat(b, c, {"model": "hijack"})
    perf = measured(Reply("one answer", {"usage": {"output": 20}}), 4)
    s.finish(jid, "one answer", performance=perf)
    s.submit(a, c, str(uuid.uuid4()), "two")
    assert s.claim()["model"] == "third"
    s.db.close()
    s = Store(path)
    assert s.chat(a, c)["model"] == "third"
    assert s.jobs(a, c)[0]["performance"] == perf
    assert s.jobs(a, c)[0]["reply"] == "one answer"


def test_legacy_migration_backup_and_no_fabricated_usage(tmp_path):
    path = tmp_path / "old.sqlite3"
    s = Store(path)
    a = s.pair(s.invite()[0], "owner")["device_id"]
    c = s.create_chat(a, "old-model")["id"]
    s.submit(a, c, str(uuid.uuid4()), "keep")
    with s.db:
        s.db.execute("ALTER TABLE jobs DROP COLUMN model_snapshot")
        s.db.execute("ALTER TABLE jobs DROP COLUMN performance")
        s.db.execute("ALTER TABLE room_jobs DROP COLUMN performance")
    s.db.close()
    s = Store(path)
    assert s.claim()["model"] == "old-model"
    assert s.jobs(a, c)[0]["performance"] is None
    backup = sqlite3.connect(tmp_path / "old.before-performance.sqlite3")
    assert backup.execute("SELECT text FROM jobs").fetchone()[0] == "keep"
    assert "model_snapshot" not in [x[1] for x in backup.execute("PRAGMA table_info(jobs)")]
    backup.close()
    s.db.close()
    s = Store(path)  # idempotent migration
    assert len(s.jobs(a, c)) == 1


def test_room_metrics_follow_visibility_and_epoch(tmp_path):
    s, a, b, _, r = setup(tmp_path)
    send(s, a, r, ask=True)
    job = s.rooms.claim()
    perf = measured(Reply("hi", {"usage": {"output": 8}}), 2)
    s.rooms.finish(job, "hi", performance=perf)
    assert s.rooms.jobs(a["device_id"], r["id"])[0]["performance"] == perf
    join(s, a, b, r)
    assert s.rooms.jobs(b["device_id"], r["id"]) == []
    send(s, b, r, ask=True)
    job = s.rooms.claim()
    s.rooms.membership(a["device_id"], r["id"], b["device_id"])
    s.rooms.finish(job, "must not publish", performance=perf)
    assert s.rooms.jobs(a["device_id"], r["id"])[-1]["performance"] is None


async def test_switch_http_auth_and_worker_measurement(tmp_path, aiohttp_client):
    ready, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def runner(job):
        calls.append(job["model"])
        ready.set()
        await release.wait()
        return Reply("answer", {"usage": {"output": 15}})

    app = make_app(tmp_path, {"agent": "main", "models": ["a", "b"], "default_model": "a"}, runner)
    client = await aiohttp_client(app)
    s = app["store"]
    a = s.pair(s.invite()[0], "owner")
    b = s.pair(s.invite()[0], "other")
    m = s.pair(s.invite(scope="member")[0], "member")
    auth = {"Authorization": "Bearer " + a["token"]}
    cid = s.create_chat(a["device_id"], "a")["id"]
    path = f"/chats/{cid}"
    assert (await client.patch(path, headers=auth, json={"model": "bad"})).status == 409
    assert (
        await client.patch(
            path, headers={"Authorization": "Bearer " + b["token"]}, json={"model": "b"}
        )
    ).status == 404
    assert (
        await client.patch(
            path, headers={"Authorization": "Bearer " + m["token"]}, json={"model": "b"}
        )
    ).status == 403
    await client.post(
        path + "/messages", headers=auth, json={"id": str(uuid.uuid4()), "text": "one"}
    )
    await asyncio.wait_for(ready.wait(), 3)
    assert (await client.patch(path, headers=auth, json={"model": "b"})).status == 200
    release.set()
    for _ in range(60):
        jobs = await (await client.get(path + "/messages", headers=auth)).json()
        if jobs[0]["state"] == "completed":
            break
        await asyncio.sleep(0.05)
    assert calls == ["a"]
    assert jobs[0]["performance"]["output_tokens"] == 15
    assert jobs[0]["performance"]["elapsed_seconds"] > 0
    assert set(jobs[0]["performance"]) == {
        "output_tokens",
        "input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "elapsed_seconds",
        "basis",
    }


def test_input_includes_reported_cache_without_total_double_counting():
    r = Reply(
        "ok",
        {
            "result": {
                "meta": {
                    "agentMeta": {
                        "usage": {
                            "input": 100,
                            "cacheRead": 80,
                            "cacheWrite": 20,
                            "output": 10,
                            "total": 210,
                        }
                    }
                }
            }
        },
    )
    m = measured(r, 2)
    assert m["input_tokens"] == 200
    assert m["cache_read_tokens"] == 80
    assert m["cache_write_tokens"] == 20
    assert m["output_tokens"] == 10
    assert measured(Reply("ok", {"usage": {"input": 20}}), 1)["input_tokens"] == 20
    assert measured(Reply("ok", {"usage": {"cacheRead": 80}}), 1)["input_tokens"] is None
    assert measured(Reply("ok", {}), 1)["cache_read_tokens"] is None
    for value in [True, -1, "15", 1.5]:
        assert measured(Reply("ok", {"usage": {"input": value}}), 1)["input_tokens"] is None
