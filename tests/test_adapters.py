import asyncio
import json
import uuid
from pathlib import Path

import pytest

from pocket.adapters import create_runner
from pocket.adapters.pi import extract_pi_reply
from pocket.cli import initialize
from pocket.performance import measured


def stream(text="hello", **overrides):
    message = {
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": "private"}, {"type": "text", "text": text}],
        "stopReason": "stop",
        "usage": {"input": 20, "output": 4, "cacheRead": 10, "cacheWrite": 0},
    }
    message.update(overrides)
    return (
        json.dumps({"type": "message_end", "message": message}, ensure_ascii=False)
        + "\n"
        + json.dumps({"type": "agent_settled", "aborted": False})
        + "\n"
    ).encode()


def test_pi_protocol_final_usage_and_fail_closed():
    reply = extract_pi_reply(stream("line\u2028next"))
    assert reply == "line\u2028next"
    assert "private" not in reply
    assert measured(reply, 2)["input_tokens"] == 30
    assert measured(reply, 2)["output_tokens"] == 4
    for raw in [
        stream(stopReason="error"),
        b'{"type":"agent_end"}\n',
        stream() + b'{"type":"agent_settled","aborted":true}\n',
        stream(content=[{"type": "toolCall", "name": "bash"}]),
        stream() + b'{"type":"tool_execution_start"}\n',
    ]:
        with pytest.raises(ValueError):
            extract_pi_reply(raw)


async def test_pi_private_sessions_arguments_and_cleanup(tmp_path, monkeypatch):
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return stream(), b""

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        prompt = Path(args[-1][1:])
        assert prompt.read_text() == "prompt content"
        assert prompt.stat().st_mode & 0o777 == 0o600
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    cfg = {"backend": "pi", "pi": "/bin/pi", "models": ["ollama/model"]}
    runner = create_runner(cfg, tmp_path)
    job = {
        "device": str(uuid.uuid4()),
        "chat": str(uuid.uuid4()),
        "model": "ollama/model",
        "text": "prompt content",
    }
    assert await runner(job) == "hello"
    assert await runner(job) == "hello"
    first = calls[0][0]
    assert first[first.index("--session") + 1] == calls[1][0][calls[1][0].index("--session") + 1]
    assert not Path(first[-1][1:]).exists()
    assert "prompt content" not in first
    for flag in ["--no-tools", "--no-mcp", "--no-extensions", "--no-skills", "--no-context-files"]:
        assert flag in first
    await runner(dict(job, device=str(uuid.uuid4())))
    assert calls[2][1]["cwd"] != calls[0][1]["cwd"]
    for patch in [
        {"device": "../../escape"},
        {"model": "unauthorized"},
        {"attachment_image": "/tmp/x.png"},
    ]:
        with pytest.raises(ValueError):
            await runner(dict(job, **patch))
    with pytest.raises(ValueError):
        create_runner({"backend": "other"}, tmp_path)


def test_pi_init_without_openclaw_or_credentials_copy(tmp_path, monkeypatch):
    monkeypatch.setattr("pocket.cli.shutil.which", lambda name: "/bin/pi" if name == "pi" else None)
    monkeypatch.setattr(
        "pocket.cli.subprocess.check_output",
        lambda *a, **k: (
            "provider model context max-out thinking images\nollama model 32K 4K no no\n"
        ),
    )
    cfg = initialize(tmp_path / "host", "127.0.0.1", 18944, "main", "pi", "ollama/model")
    assert cfg["backend"] == "pi"
    assert "openclaw" not in cfg
    assert cfg["models"] == ["ollama/model"]
    assert not any("auth" in p.name for p in (tmp_path / "host").iterdir())
    with pytest.raises(ValueError):
        initialize(tmp_path / "host", "127.0.0.1", 18944, "main", "pi")
