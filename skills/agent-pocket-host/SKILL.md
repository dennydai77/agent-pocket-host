---
name: agent-pocket-host
description: 安裝或操作 Agent Pocket 獨立主機服務；為 OpenClaw 或 pi 產生手机配對 QR、查詢連線狀態及撤銷裝置。不適用 Lumen 配對或 OpenClaw 原生 node 配對。
---

# Agent Pocket Host

使用本 skill 附屬的 `scripts/host.py` 呼叫同 checkout 的服務；相對路徑以本 skill 目錄為基準。先讀 repo 根目錄 README.md 與 docs/ADAPTERS.md。若 skill 被複製而非 symlink，wrapper 會拒絕；需指定環境變數 `AGENT_POCKET_HOST_HOME` 指向完整 checkout。

## 初始化與啟動

1. 先查 `scripts/host.py --help`、現有 state/config 是否存在及佔用埠；使用者已有服務時保留它，獨立 state／port 預設8944。完成條件：確認 backend、主機 Tailscale IP、state 與模型。
2. 尚未初始化時執行 `scripts/host.py --state <獨立目錄> init --host <本機IP> --backend openclaw|pi --model <provider/model>`。不替使用者下載模型或搬移憑證。完成條件：初始化成功、未覆蓋其他服務。
3. 執行相同 state 的 `serve`，再用 `status` 驗證 HTTPS。pi 此版僅私人文字／無工具；不加 `--room-agent local`。完成條件：health 可達，backend 與設定相符。

## 配對與撤銷

- 先說明 owner 配對可使用私人 Agent，不等同群聊邀請；member 只配對群聊成員。
- 服務確認可達後再執行 `invite --open`（或 `invite --scope member`）。QR 有效120秒／一次使用。只在請求人私訊或自己的本機畫面交付，公開群組不貼 payload、QR 或票據。
- 不把成功產生 QR 當作手機配對完成；確認第一筆配對與訊息實際儲存後才請人繼續。
- App 0.7.1 仍有其他主機自簽 TLS 限制；須明示未完成跨主機驗收，不以關閉驗證繞過。
- `devices` 列出裝置後，依使用者指定的精確 ID 執行 `revoke <id>`，再核對 revoked；不順便撤銷其他裝置。

## 驗證與界線

新增或變更程式後在 repo 跑 `uv run --frozen pytest tests -q`；主機操作用 status 驗證，不印認證檔或完整 private config。缺模型／登入／不相容 pi 版本時回報原因，不換成另一個人的 Agent 或靜默 fallback。

本 Skill 不自動上傳 GitHub、不修改 Gateway／ACL、不安裝登入常駐服務，也不搬動原 Agent Pocket 的資料。公開發佈另行處理，只納入程式、測試與文件。
