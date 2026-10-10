import sqlite3
import uuid

import pytest

from pocket.core import Store
from pocket.server import make_app


def identities(store):
    return [store.pair(store.invite()[0], n)["device_id"] for n in ("A", "B")]


def test_migrates_legacy_without_losing_history(tmp_path):
    path = tmp_path / "pocket.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE devices(id TEXT PRIMARY KEY, hash TEXT UNIQUE, name TEXT, revoked INTEGER DEFAULT 0);
    CREATE TABLE chats(id TEXT PRIMARY KEY, device TEXT REFERENCES devices(id), title TEXT, model TEXT, created REAL);
    CREATE TABLE jobs(id TEXT PRIMARY KEY, chat TEXT REFERENCES chats(id), device TEXT REFERENCES devices(id), text TEXT, state TEXT, reply TEXT DEFAULT '', error TEXT DEFAULT '', created REAL);
    INSERT INTO devices VALUES ('A','hash','owner',0);
    INSERT INTO chats VALUES ('chat','A','old title','model',1);
    INSERT INTO jobs VALUES ('job','chat','A','old message','completed','old reply','',1);
    """)
    db.close()
    s = Store(path)
    assert s.chat("A", "chat")["group_id"] is None
    assert s.jobs("A", "chat")[0]["reply"] == "old reply"
    assert (tmp_path / "pocket.before-topics.sqlite3").exists()
    s.db.close()
    reopened = Store(path)
    assert len(reopened.chats("A")) == 1
    assert reopened.jobs("A", "chat")[0]["text"] == "old message"


def test_group_crud_move_and_delete_keeps_conversation(tmp_path):
    path = tmp_path / "db"
    s = Store(path)
    a, b = identities(s)
    g = s.create_group(a, " 工作 ")
    assert s.create_group(a, "工作")["id"] == g["id"]
    assert s.create_group(b, "工作")["id"] != g["id"]
    other = s.create_group(a, "生活")
    with pytest.raises(sqlite3.IntegrityError):
        s.rename_group(a, g["id"], "生活")
    c = s.create_chat(a, "model", g["id"])
    s.submit(a, c["id"], str(uuid.uuid4()), "first")
    s.finish(s.jobs(a, c["id"])[0]["id"], reply="reply")
    s.update_chat(a, c["id"], {"group_id": other["id"], "title": "有意義的頻道名稱"})
    assert s.chat(a, c["id"])["id"] == c["id"]
    assert s.jobs(a, c["id"])[0]["reply"] == "reply"
    s.delete_group(a, other["id"])
    assert s.chat(a, c["id"])["group_id"] is None
    assert s.chat(a, c["id"])["title"] == "有意義的頻道名稱"
    assert len(s.jobs(a, c["id"])) == 1
    s.db.close()
    reopened = Store(path)
    assert reopened.chat(a, c["id"])["group_id"] is None
    assert len(reopened.groups(a)) == 1


def test_cross_device_topic_access_denied(tmp_path):
    s = Store(tmp_path / "db")
    a, b = identities(s)
    g = s.create_group(a, "A private")
    cb = s.create_chat(b, "model")
    for call in [
        lambda: s.rename_group(b, g["id"], "changed"),
        lambda: s.delete_group(b, g["id"]),
        lambda: s.create_chat(b, "model", g["id"]),
        lambda: s.update_chat(b, cb["id"], {"group_id": g["id"]}),
    ]:
        with pytest.raises(KeyError):
            call()
    assert s.groups(b) == []
    assert s.chat(b, cb["id"])["group_id"] is None


def test_manual_title_and_validation(tmp_path):
    s = Store(tmp_path / "db")
    a, _ = identities(s)
    c = s.create_chat(a, "model")
    s.update_chat(a, c["id"], {"title": "新對話"})
    s.submit(a, c["id"], str(uuid.uuid4()), "must not replace manual title")
    assert s.chat(a, c["id"])["title"] == "新對話"
    for name in ["", " ", "a" * 61, 42]:
        with pytest.raises(ValueError):
            s.create_group(a, name)
    with pytest.raises(ValueError):
        s.update_chat(a, c["id"], {"unknown_field": "not allowed"})


async def test_topics_http_contract(tmp_path, aiohttp_client):
    async def runner(job):
        return "reply"

    app = make_app(
        tmp_path, {"agent": "main", "models": ["model"], "default_model": "model"}, runner
    )
    client = await aiohttp_client(app)
    p = app["store"].pair(app["store"].invite()[0], "A")
    auth = {"Authorization": "Bearer " + p["token"]}
    assert (await client.get("/groups")).status == 401
    settings = await (await client.get("/settings", headers=auth)).json()
    assert "topic_groups" in settings["capabilities"]
    g = await (await client.post("/groups", json={"name": "Work"}, headers=auth)).json()
    assert (
        await client.patch("/groups/" + g["id"], json={"name": "工作"}, headers=auth)
    ).status == 200
    assert (
        await client.post("/chats", json={"model": "model", "group_id": {}}, headers=auth)
    ).status == 409
    c = await (
        await client.post("/chats", json={"model": "model", "group_id": g["id"]}, headers=auth)
    ).json()
    assert c["group_id"] == g["id"]
    assert (
        await client.patch("/chats/" + c["id"], json={"title": "測試"}, headers=auth)
    ).status == 200
    assert (await client.delete("/groups/" + g["id"], headers=auth)).status == 200
    chats = await (await client.get("/chats", headers=auth)).json()
    assert chats[0]["group_id"] is None
    assert chats[0]["title"] == "測試"
