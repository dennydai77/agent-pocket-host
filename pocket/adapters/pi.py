"""Pi JSONL adapter. Dedicated sessions, text-only, tools/resources disabled."""

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path

from ..performance import Reply


def extract_pi_reply(raw):
    # JSONL is LF-delimited; splitlines would corrupt valid Unicode separators.
    events = [json.loads(line) for line in raw.decode().split("\n") if line.strip()]
    settled = [e for e in events if e.get("type") == "agent_settled"]
    if not settled or settled[-1].get("aborted"):
        raise ValueError("pi 未確認完成，不會自動重送")
    messages = [
        e["message"]
        for e in events
        if e.get("type") == "message_end" and e.get("message", {}).get("role") == "assistant"
    ]
    if not messages or messages[-1].get("stopReason") in ("error", "aborted", "toolUse"):
        raise ValueError("pi 沒有成功回答")
    if any(e.get("type", "").startswith("tool_execution_") for e in events):
        raise ValueError("pi 工具政策驗證失敗")
    if any(c.get("type") == "toolCall" for m in messages for c in m.get("content", [])):
        raise ValueError("pi 工具政策驗證失敗")
    text = "\n".join(
        c["text"]
        for c in messages[-1].get("content", [])
        if c.get("type") == "text" and isinstance(c.get("text"), str)
    ).strip()
    if not text or len(text) > 100000:
        raise ValueError("pi 文字回答為空或超限")
    usage = {}
    for key in ("input", "output", "cacheRead", "cacheWrite"):
        values = [(m.get("usage") or {}).get(key) for m in messages]
        if all(type(v) is int and 0 <= v < 10**12 for v in values):
            usage[key] = sum(values)
    return Reply(text, {"usage": usage})


class Pi:
    def __init__(self, config, state):
        self.config = config
        self.root = Path(state) / "pi-sessions"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    async def __call__(self, job):
        if job.get("attachment_image"):
            raise ValueError("pi 第一版尚未提供圖片分析")
        if job["model"] not in self.config["models"]:
            raise ValueError("pi 模型未授權")
        device, chat = str(uuid.UUID(job["device"])), str(uuid.UUID(job["chat"]))
        folder = self.root / device / chat
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix="prompt-", suffix=".txt", dir=folder)
        proc = None
        try:
            with os.fdopen(fd, "w") as f:
                f.write(job["text"])
            proc = await asyncio.create_subprocess_exec(
                self.config["pi"],
                "--offline",
                "--mode",
                "json",
                "--print",
                "--no-tools",
                "--no-extensions",
                "--no-mcp",
                "--no-skills",
                "--no-prompt-templates",
                "--no-context-files",
                "--no-approve",
                "--thinking",
                "off",
                "--model",
                job["model"],
                "--session",
                str(folder / "session.jsonl"),
                "--",
                "@" + name,
                cwd=folder,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                raw, _ = await asyncio.wait_for(proc.communicate(), 330)
            except TimeoutError:
                raise RuntimeError("pi 逾時，結果未確認；未自動重送") from None
            if proc.returncode or len(raw) > 4_194_304:
                raise RuntimeError("pi 執行失敗或回覆超限；請檢查本機模型與登入狀態")
            return extract_pi_reply(raw)
        finally:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            Path(name).unlink(missing_ok=True)
