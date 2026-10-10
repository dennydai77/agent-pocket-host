import asyncio
import uuid

import pytest
from pocket.core import Store
from pocket.rooms import RoomError
from pocket.server import make_app


def id():
    return str(uuid.uuid4())


def setup(tmp_path):
    s = Store(tmp_path / "db")
    a = s.pair(s.invite()[0], "Owner")
    b = s.pair(s.invite(scope="member")[0], "Member")
    c = s.pair(s.invite(scope="member")[0], "Other")
    r = s.rooms.create(
        a["device_id"], {"client_id": id(), "title": "Room", "model": "model"}, ["model"]
    )
    return s, a, b, c, r


def join(s, a, b, r):
    i = s.rooms.invite(a["device_id"], r["id"])
    return s.rooms.invitation(b["device_id"], i["token"], True)


def send(s, a, r, text="hello", ask=False, cid=None, reply=None):
    return s.rooms.send(
        a["device_id"],
        r["id"],
        {"client_id": cid or id(), "text": text, "ask_agent": ask, "reply_to_id": reply},
        True,
    )


def test_members_invites_history_and_idempotency(tmp_path):
    s, a, b, c, r = setup(tmp_path)
    old = send(s, a, r, "old private-to-existing-members")
    invite = s.rooms.invite(a["device_id"], r["id"])
    assert s.rooms.invitation(b["device_id"], invite["token"])["history"] == "加入後訊息"
    s.rooms.invitation(b["device_id"], invite["token"], True)
    s.rooms.invitation(b["device_id"], invite["token"], True)
    assert s.rooms.messages(b["device_id"], r["id"]) == []
    with pytest.raises(RoomError):
        s.rooms.message(b["device_id"], r["id"], old["id"])
    with pytest.raises(RoomError):
        s.rooms.invitation(c["device_id"], invite["token"], True)
    with pytest.raises(RoomError):
        s.rooms.messages(c["device_id"], r["id"])
    cid = id()
    m = send(s, b, r, cid=cid)
    assert send(s, b, r, cid=cid)["id"] == m["id"]
    with pytest.raises(RoomError):
        send(s, b, r, "changed", cid=cid)
    assert s.rooms.messages(a["device_id"], r["id"])[-1]["author_name"] == "Member"
    s.rooms.membership(a["device_id"], r["id"], b["device_id"], 1)
    assert s.rooms.messages(b["device_id"], r["id"])[0]["id"] == old["id"]
    s.rooms.membership(a["device_id"], r["id"], b["device_id"])
    with pytest.raises(RoomError):
        s.rooms.messages(b["device_id"], r["id"])
    s.db.close()
    reopened = Store(tmp_path / "db")
    assert len(reopened.rooms.messages(a["device_id"], r["id"])) == 2
    assert reopened.rooms.scope(b["device_id"]) == "member"


def test_agent_visibility_races_and_revocation(tmp_path):
    s, a, b, c, r = setup(tmp_path)
    send(s, a, r, "OLD_SENTINEL", ask=True)
    j = s.rooms.claim()
    join(s, a, b, r)
    s.rooms.finish(j, "old answer")
    assert s.rooms.jobs(a["device_id"], r["id"])[0]["state"] == "superseded"
    send(s, b, r, "new question", ask=True)
    j = s.rooms.claim()
    assert "OLD_SENTINEL" not in str(j["messages"])
    s.rooms.finish(j, "new answer")
    assert s.rooms.messages(b["device_id"], r["id"])[-1]["text"] == "new answer"
    send(s, b, r, "later", ask=True)
    j = s.rooms.claim()
    s.rooms.revoke_device(b["device_id"])
    s.rooms.finish(j, "should not publish")
    assert "should not publish" not in str(s.rooms.messages(a["device_id"], r["id"]))
    send(s, a, r, "restart", ask=True)
    s.rooms.claim()
    s.rooms.recover()
    assert s.rooms.jobs(a["device_id"], r["id"])[-1]["state"] == "interrupted"


def test_references_cannot_launder_hidden_history(tmp_path):
    s, a, b, c, r = setup(tmp_path)
    old = send(s, a, r, "secret")
    join(s, a, b, r)
    with pytest.raises(RoomError):
        send(s, b, r, reply=old["id"])
    derived = send(s, a, r, "reference", reply=old["id"])
    assert derived["source_from"] == old["seq"]
    assert s.rooms.messages(b["device_id"], r["id"]) == []
    with pytest.raises(RoomError):
        s.rooms.enqueue(b["device_id"], r["id"], derived["id"])
    with pytest.raises(RoomError):
        s.rooms.membership(b["device_id"], r["id"], a["device_id"])
    with pytest.raises(RoomError):
        s.rooms.membership(a["device_id"], r["id"], a["device_id"])


def test_expired_invitation_and_archive(tmp_path):
    s, a, b, c, r = setup(tmp_path)
    i = s.rooms.invite(a["device_id"], r["id"])
    with s.db:
        s.db.execute("UPDATE room_invites SET expires=0")
    with pytest.raises(RoomError):
        s.rooms.invitation(b["device_id"], i["token"], True)
    s.rooms.update(a["device_id"], r["id"], {"state": "archived"}, ["model"])
    with pytest.raises(RoomError):
        send(s, a, r)
    assert s.rooms.messages(a["device_id"], r["id"]) == []


async def test_http_member_cannot_use_main_and_rooms_work(tmp_path, aiohttp_client):
    calls = []

    async def private(j):
        calls.append(j)
        return "private"

    async def shared(j):
        return "ROOM_OK"

    config = {
        "agent": "main",
        "models": ["model"],
        "default_model": "model",
        "origin": "https://100.80.1.2:8943",
        "certificate_sha256": "a" * 64,
    }
    app = make_app(tmp_path, config, private, shared)
    client = await aiohttp_client(app)
    s = app["store"]
    a = s.pair(s.invite()[0], "A")
    b = s.pair(s.invite(scope="member")[0], "B")
    ha = {"Authorization": "Bearer " + a["token"]}
    hb = {"Authorization": "Bearer " + b["token"]}
    info = await (await client.get("/settings", headers=hb)).json()
    assert info["private_chat"] is False and info["agent"] != "main"
    assert (await client.get("/chats", headers=hb)).status == 403
    assert (await client.post("/chats", headers=hb, json={})).status == 403
    r = await (
        await client.post(
            "/v1/rooms", headers=ha, json={"client_id": id(), "title": "HTTP", "model": "model"}
        )
    ).json()
    prefix = "/v1/rooms/" + r["id"]
    i = await (await client.post(prefix + "/invites", headers=ha, json={})).json()
    assert "pocket-room-invite-v1" in i["qr"]
    assert (
        await client.post("/v1/room-invites/accept", headers=hb, json={"token": i["token"]})
    ).status == 200
    assert (
        await client.post(
            prefix + "/messages",
            headers=hb,
            json={"client_id": id(), "text": "hi", "ask_agent": "true"},
        )
    ).status == 200
    for _ in range(30):
        messages = await (await client.get(prefix + "/messages", headers=ha)).json()
        if len(messages) == 2:
            break
        await asyncio.sleep(0.02)
    assert messages[-1]["text"] == "ROOM_OK" and not calls
    assert (await client.delete(prefix + "/members/" + b["device_id"], headers=ha)).status == 200
    assert (await client.get(prefix + "/messages", headers=hb)).status == 404


def test_room_placement_is_personal_and_topics_deletion_preserves_room(tmp_path):
    s, a, b, c, r = setup(tmp_path)
    join(s, a, b, r)
    ga = s.create_group(a["device_id"], "Work")
    gb = s.create_group(b["device_id"], "Life")
    with s.db:
        s.db.execute(
            "INSERT INTO room_placements VALUES(?,?,?)", (r["id"], a["device_id"], ga["id"])
        )
        s.db.execute(
            "INSERT INTO room_placements VALUES(?,?,?)", (r["id"], b["device_id"], gb["id"])
        )
    s.delete_group(a["device_id"], ga["id"])
    assert s.rooms.listing(a["device_id"])[0]["group_id"] is None
    assert s.rooms.listing(b["device_id"])[0]["group_id"] == gb["id"]
    send(s, b, r)
    assert len(s.rooms.messages(a["device_id"], r["id"])) == 1


def test_disabled_agent_is_atomic_and_author_spoof_is_rejected(tmp_path):
    s, a, b, c, r = setup(tmp_path)
    with pytest.raises(RoomError) as error:
        s.rooms.send(
            a["device_id"],
            r["id"],
            {"client_id": id(), "text": "question", "ask_agent": True},
            False,
        )
    assert error.value.status == 503
    assert s.rooms.messages(a["device_id"], r["id"]) == []
    with pytest.raises(RoomError):
        s.rooms.send(
            a["device_id"], r["id"], {"client_id": id(), "text": "spoof", "author": b["device_id"]}
        )
    assert s.rooms.messages(a["device_id"], r["id"]) == []
    assert s.db.execute("SELECT count(*) FROM room_jobs").fetchone()[0] == 0
