# Extraction origin

Extracted 2026-10-10 from Agent Pocket host commit `1434660` (App v0.7.1).
Copied only Python host sources, dependency lock and synthetic tests; not iOS source, runtime, credentials, pairing codes or Git history.

Changes here: independent package/CLI, backend factory, separate OpenClaw adapter, experimental pi adapter, configurable initial model, status command, default port8944, repo-owned operational Skill.

Pi integration reuses the installed CLI's documented JSONL protocol rather than copying its source. Tested CLI identity: @earendil-works/pi-coding-agent 1.1.0. The bridge's Python dependencies remain the existing maintained aiohttp/cryptography/segno/Pillow/pypdf libraries, pinned in uv.lock.

The old host remains deployed separately. A later migration must preserve existing pairing identity and data; there is no automatic sync or migration between the two repositories.
