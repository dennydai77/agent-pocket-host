#!/usr/bin/env python3
"""Resolve the real checkout even when the skill directory is symlinked."""
import os
from pathlib import Path

root = Path(os.environ.get("AGENT_POCKET_HOST_HOME", Path(__file__).resolve().parents[3])).resolve()
entry = root / "scripts/pocket-host"
if not entry.is_file() or not (root / "pocket/cli.py").is_file():
    raise SystemExit("完整 checkout 不存在；請設定 AGENT_POCKET_HOST_HOME，不要只複製 SKILL.md")
os.execv(str(entry), [str(entry), *os.sys.argv[1:]])
