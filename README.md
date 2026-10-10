# Agent Pocket Host

Agent Pocket 手機 App 的獨立主機服務：QR 配對、裝置授權、私人對話、群聊、附件與 token 用量。手機 → Tailscale → Host → **OpenClaw 或 pi**。

從 Agent Pocket v0.7.1 主機端抽出，獨立 Git／Python 環境／資料目錄。**不是只畫 QR，也不直接相容 Lumen QR 或 OpenClaw 原生 node QR。**

## 支援狀態

- **OpenClaw adapter**：原有私人 Agent、同對話模型切換、附件明確分析；可選隔離本地群聊 Gemma。
- **pi adapter（實驗）**：私人文字對話、每個裝置／對話獨立 session、模型選擇與用量。初版停用工具、MCP、extensions、skills 及 context-file 自動載入；保留 pi 既有供應商登入，不複製金鑰。支援附件傳輸及可解析文字分析；**圖片分析與群聊 Agent 尚未開放**。
- **傳輸協定 v1**：相容 Agent Pocket 0.7.1；用途與已驗證範圍見 [STATUS](docs/STATUS.md)。
- 現有 App 的自簽 ATS 例外僅涵蓋原開發主機：其他主機即使 Host 可正常產生 QR，仍可能被 iOS 拒絕。**跨主機 TLS 尚待 App 端改造與真機驗證，不能宣稱任意機器即掃即用。**

## 快速開始（macOS／Linux）

需要 Python 3.12+、uv、Tailscale，以及已設定模型的 `openclaw` 或 `pi` CLI。初版用 fcntl 鎖檔，尚不支援 Windows；本次驗證於 macOS。

```sh
uv sync --frozen
# OpenClaw：填寫這台主機自己的 Tailscale IPv4，不使用別人的配對資料
./scripts/pocket-host init --host <本機-Tailscale-IP> --backend openclaw --model <provider/model>
# 或 pi（不要在同一個已初始化 state 上重跑）
./scripts/pocket-host --state .runtime/pi-host init --host <本機-Tailscale-IP> --backend pi --model ollama/gemma4:12b
./scripts/pocket-host --state .runtime/pi-host serve
```

預設埠 **8944**，與原 App 開發服務分開。預設 state 為本 checkout 的 `.runtime/host`；不同 backend 各自初始化 state，不能共享資料庫。`init` 遇既有設定拒絕覆蓋；不自動安裝 daemon、不改 Tailscale ACL、Gateway 或其他 App。

另開終端：

```sh
./scripts/pocket-host --state .runtime/pi-host status
./scripts/pocket-host --state .runtime/pi-host invite --open
./scripts/pocket-host --state .runtime/pi-host devices
./scripts/pocket-host --state .runtime/pi-host revoke <device-id>
```

QR 有效 120 秒、一次使用，檔案保留於私有 state，不在終端列出票據。`--scope member` 只發群聊成員配對；邀請進聊天室是另一個步驟。Owner QR 具有私人 Agent 權限，不要當成群聊邀請轉給別人。

OpenClaw 群聊需既有本地 gemma4:12b，可用 `serve --room-agent local`；pi 若指定此選項會拒絕，不回退私人 OpenClaw。

## Skill

操作指引在 [skills/agent-pocket-host/SKILL.md](skills/agent-pocket-host/SKILL.md)。它呼叫同 repo 的 CLI，不藏入你的憑證。

- pi 可用 `pi --skill /absolute/path/agent-pocket-host/skills/agent-pocket-host` 載入。
- OpenClaw 可將該 skill 目錄連結到其 skills 目錄（依安裝環境選擇）；保留 checkout，不只複製 SKILL.md。附屬 wrapper 會解開 symlink 找 repo。
- 這是管理 Host 的 Skill；不是讓手機開啟 pi 工具權限的開關。

## 開發與發佈

```sh
uv run --frozen pytest tests -q
uv run --frozen ruff check pocket
uv build
```

Backend 邊界見 [ADAPTERS](docs/ADAPTERS.md)，傳輸規格见 [PROTOCOL](docs/PROTOCOL.md)。原始碼與 lockfile 可獨立發佈；`.runtime`、配對圖片／JSON、session、私鑰、資料庫及憑證不得加入 Git。

## 授權

[MIT](LICENSE) © 2026 DennyDai
