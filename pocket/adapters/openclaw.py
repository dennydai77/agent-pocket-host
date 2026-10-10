import asyncio
import json
import os
import tempfile
from pathlib import Path

from ..core import extract_reply
from ..performance import Reply


class OpenClaw:
    def __init__(self, config: dict, state: Path):
        self.config, self.state = config, state

    async def __call__(self, job: dict):
        # A dedicated owner-authorized session per device + conversation; no Discord delivery.
        session = f"agent:{self.config['agent']}:pocket:{job['device']}:{job['chat']}"
        fd, name = tempfile.mkstemp(prefix="message-", suffix=".txt", dir=self.state)
        proc = None
        try:
            with os.fdopen(fd, "w") as f:
                f.write(
                    "使用者透過 Agent-pocket 私人手機介面傳送訊息。請直接回覆文字；不要透過 Discord 或其他通道傳送本次回答。\n\n"
                    + job["text"]
                    + (
                        "\n本次使用者明確授權分析的圖片：\n" + job["attachment_image"]
                        if job.get("attachment_image")
                        else ""
                    )
                )
            proc = await asyncio.create_subprocess_exec(
                self.config["openclaw"],
                "agent",
                "--agent",
                self.config["agent"],
                "--session-key",
                session,
                "--model",
                job["model"],
                "--message-file",
                name,
                "--json",
                "--timeout",
                "300",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=330)
            except TimeoutError:
                raise RuntimeError("等待 OpenClaw 逾時；遠端結果未確認，不會自動重送。") from None
            if proc.returncode:
                raise RuntimeError("OpenClaw 執行失敗；請檢查 Mac 端狀態。未自動重送。")
            if len(stdout) > 4_194_304:
                raise RuntimeError("OpenClaw 回覆超出大小上限")
            return Reply(extract_reply(stdout), json.loads(stdout))
        finally:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            Path(name).unlink(missing_ok=True)
