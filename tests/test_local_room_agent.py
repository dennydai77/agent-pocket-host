import uuid

import pytest

from pocket.local_room_agent import MODEL, build_prompt, isolated_config
from pocket.server import make_app


def test_fixed_policy_and_context_budget():
    cfg = isolated_config()
    assert cfg["plugins"]["allow"] == ["ollama"]
    assert cfg["plugins"]["slots"]["memory"] == "none"
    assert cfg["tools"]["deny"] == ["*"]
    assert cfg["tools"]["fs"]["workspaceOnly"]
    assert cfg["agents"]["defaults"]["contextInjection"] == "never"
    trigger = {"id": "target", "text": "answer this", "author": "A", "kind": "human", "seq": 1}
    job = {
        "trigger_id": "target",
        "trigger_message": trigger,
        "messages": [{"id": str(i), "text": "舊" * 1000} for i in range(40)],
    }
    prompt = build_prompt(job)
    assert "answer this" in prompt and len(prompt) < 8000
    trigger["text"] = "x" * 6001
    with pytest.raises(ValueError):
        build_prompt(job)


async def test_local_mode_is_per_room_and_private_models_unchanged(tmp_path, aiohttp_client):
    async def run(job):
        return "local answer"

    config = {
        "agent": "main",
        "models": ["cloud"],
        "default_model": "cloud",
        "room_models": ["none", MODEL],
        "room_default_model": "none",
    }
    app = make_app(tmp_path, config, room_runner=run)
    client = await aiohttp_client(app)
    s = app["store"]
    a = s.pair(s.invite()[0], "A")
    auth = {"Authorization": "Bearer " + a["token"]}
    settings = await (await client.get("/settings", headers=auth)).json()
    assert settings["models"] == ["cloud"] and settings["default_model"] == "cloud"
    assert settings["room_models"] == ["none", MODEL]
    r = await (
        await client.post(
            "/v1/rooms",
            headers=auth,
            json={"client_id": str(uuid.uuid4()), "title": "Room", "model": "none"},
        )
    ).json()
    path = "/v1/rooms/" + r["id"]
    payload = {"client_id": str(uuid.uuid4()), "text": "hi", "ask_agent": True}
    assert (await client.post(path + "/messages", headers=auth, json=payload)).status == 503
    assert (await client.patch(path, headers=auth, json={"model": MODEL})).status == 200
    assert (await client.post(path + "/messages", headers=auth, json=payload)).status == 200
    assert (await client.patch(path, headers=auth, json={"model": "cloud"})).status == 400
    private = await (await client.post("/chats", headers=auth, json={"model": "cloud"})).json()
    assert private["model"] == "cloud"
    assert (await client.post("/chats", headers=auth, json={"model": MODEL})).status == 409
