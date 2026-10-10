import asyncio
import json
import uuid

import pytest
from pocket.cli import valid_host
from pocket.core import Store, extract_reply
from pocket.server import make_app


def test_pairing_once_expiry_revoke(tmp_path):
    store = Store(tmp_path / "db")
    token, _ = store.invite(-1)
    with pytest.raises(ValueError):
        store.pair(token, "expired")
    token, _ = store.invite()
    pair = store.pair(token, "owner")
    with pytest.raises(ValueError):
        store.pair(token, "replay")
    assert store.device(pair["token"]) == pair["device_id"]
    assert store.device("wrong") is None
    assert store.db.execute("SELECT hash FROM devices").fetchone()[0] != pair["token"]
    with store.db:
        store.db.execute("UPDATE devices SET revoked=1")
    assert store.device(pair["token"]) is None


def test_durable_idempotency_and_isolation(tmp_path):
    path = tmp_path / "db"
    store = Store(path)
    a = store.pair(store.invite()[0], "a")["device_id"]
    b = store.pair(store.invite()[0], "b")["device_id"]
    chat = store.create_chat(a, "cloud/model")["id"]
    jid = str(uuid.uuid4())
    store.submit(a, chat, jid, "hello")
    store.submit(a, chat, jid, "hello")
    assert len(store.jobs(a, chat)) == 1
    with pytest.raises(KeyError):
        store.jobs(b, chat)
    with pytest.raises(ValueError):
        store.submit(a, chat, jid, "changed")
    with pytest.raises(ValueError):
        store.submit(a, chat, str(uuid.uuid4()), "second")
    assert store.claim()["id"] == jid
    store.db.close()
    store = Store(path)
    store.recover()
    assert store.jobs(a, chat)[0]["state"] == "interrupted"
    assert store.claim() is None
    assert store.submit(a, chat, jid, "hello")["state"] == "interrupted"


def test_revoke_prevents_queued_execution(tmp_path):
    s = Store(tmp_path / "db")
    a = s.pair(s.invite()[0], "a")["device_id"]
    c = s.create_chat(a, "model")["id"]
    s.submit(a, c, str(uuid.uuid4()), "hello")
    with s.db:
        s.db.execute("UPDATE devices SET revoked=1")
    assert s.claim() is None
    assert s.jobs(a, c)[0]["state"] == "failed"


def test_reply_filters_private_tool_fields():
    payload = {"result": {"payloads": [{"text": "hello"}], "meta": {"private": "secret"}}}
    assert extract_reply(json.dumps(payload).encode()) == "hello"
    for x in [
        {"ok": False},
        {"result": {"payloads": [{"isError": True, "text": "error"}]}},
        {"result": {"meta": {"aborted": True}}},
        {},
    ]:
        with pytest.raises(ValueError):
            extract_reply(json.dumps(x).encode())


def test_private_bind_only():
    for host in ["0.0.0.0", "192.168.1.1", "8.8.8.8", "::"]:
        with pytest.raises(ValueError):
            valid_host(host)
    for host in ["100.80.1.1", "127.0.0.1"]:
        valid_host(host)


async def test_http_flow_and_auth(tmp_path, aiohttp_client):
    calls = []

    async def runner(job):
        calls.append(job)
        return "test reply"

    app = make_app(
        tmp_path,
        {"agent": "main", "models": ["cloud/model"], "default_model": "cloud/model"},
        runner,
    )
    client = await aiohttp_client(app)
    assert (await client.get("/chats")).status == 401
    assert (await client.get("/health", headers={"Origin": "https://evil.example"})).status == 403
    token, _ = app["store"].invite()
    r = await client.post("/pair", json={"invite": token, "name": "test"})
    assert r.status == 200
    p = await r.json()
    auth = {"Authorization": "Bearer " + p["token"]}
    assert (await client.post("/pair", json={"invite": token, "name": "replay"})).status == 409
    assert (
        await client.post("/chats", json={"model": "ollama/unapproved"}, headers=auth)
    ).status == 409
    c = await (await client.post("/chats", json={"model": "cloud/model"}, headers=auth)).json()
    path = f"/chats/{c['id']}/messages"
    message = {"id": str(uuid.uuid4()), "text": "hello"}
    for _ in range(2):
        assert (await client.post(path, json=message, headers=auth)).status == 202
    for _ in range(30):
        rows = await (await client.get(path, headers=auth)).json()
        if rows[0]["state"] == "completed":
            break
        await asyncio.sleep(0.05)
    assert rows[0]["reply"] == "test reply"
    assert len(calls) == 1
    b = app["store"].pair(app["store"].invite()[0], "other")
    assert (await client.get(path, headers={"Authorization": "Bearer " + b["token"]})).status == 404
    assert (await client.post("/unpair", headers=auth)).status == 200
    assert (await client.get("/chats", headers=auth)).status == 401


async def test_cli_scope_and_temporary_file_cleanup(tmp_path, monkeypatch):
    from pathlib import Path

    from pocket.adapters.openclaw import OpenClaw

    seen = {}

    class Process:
        returncode = 0

        async def communicate(self):
            return b'{"result":{"payloads":[{"text":"ok"}]}}', b""

    async def spawn(*args, **kwargs):
        seen["args"] = args
        path = Path(args[args.index("--message-file") + 1])
        seen["path"] = path
        assert "test content" in path.read_text()
        assert path.stat().st_mode & 0o777 == 0o600
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    client = OpenClaw({"agent": "main", "openclaw": "/verified/openclaw"}, tmp_path)
    assert (
        await client(
            {
                "device": "device",
                "chat": "chat",
                "model": "openai/gpt-5.6-sol",
                "text": "test content",
            }
        )
        == "ok"
    )
    assert "--deliver" not in seen["args"]
    assert "agent:main:pocket:device:chat" in seen["args"]
    assert "--model" in seen["args"]
    assert not seen["path"].exists()


def test_generated_certificate_supports_ios_server_usage(tmp_path, monkeypatch):
    import pocket.cli
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID
    from pocket.cli import initialize

    monkeypatch.setattr(pocket.cli.shutil, "which", lambda _: "/test/openclaw")
    monkeypatch.setattr(
        pocket.cli.subprocess,
        "check_output",
        lambda *args, **kwargs: json.dumps(
            {
                "models": [
                    {
                        "key": "openai/gpt-5.6-sol",
                        "available": True,
                        "local": False,
                        "tags": ["configured"],
                    }
                ]
            }
        ).encode(),
    )
    config = initialize(tmp_path / "host", "127.0.0.1", 8944, "main")
    cert = x509.load_pem_x509_certificate((tmp_path / "host/cert.pem").read_bytes())
    assert (
        ExtendedKeyUsageOID.SERVER_AUTH
        in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    )
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert config["default_model"] == "openai/gpt-5.6-sol"
    assert (tmp_path / "host/key.pem").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        initialize(tmp_path / "host", "127.0.0.1", 8944, "main")
