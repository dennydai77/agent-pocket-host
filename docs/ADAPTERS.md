# Backend adapters

配對／bearer 裝置授權／scope／房間歷史可見性由 Host 負責，**不交給模型判斷**。`pocket.adapters.create_runner(config,state)` 選擇明確 backend，未知 backend 拒絕，不隱式 fallback。

共用 runner 契約：`async __call__(job) -> Reply`（str 子類）；job 含 `device/chat/text/model`，image 只在附件明確分析授權後出現。`Reply` 只保存回答與 input/output/cache 用量，API 不傳 provider raw metadata。worker 將耗時與用量持久保存，不自動重跑結果未知的工作。

## OpenClaw

`adapters/openclaw.py` 將 job 送至 `openclaw agent --session-key agent:<agent>:pocket:<device>:<chat> --model ... --message-file ... --json`，不加 deliver。原模型授權／工具政策沿用該 Agent。群聊仍走獨立 LocalRoomAgent，絕不重用私人 session。

## pi（本機可用 CLI 1.1.0）

使用 `pi --offline --mode json --print --session <state/pi-sessions/device/chat/session.jsonl>`，`--model provider/model`，訊息存入 0600 暫存檔以 @file 引入，結束即清除。不是 --continue（不接上其他工作），cwd 也在該對話專屬資料夾。

明確停用 tools／extensions／MCP／skills／prompt templates／context files，並忽略 project-local approval；此版定位文字介面，不宣稱完整 coding-agent 遠端控制。沿用 pi 既有供應商設定及認證，這代表需在 Host 的系統帳號事先完成登入。`--offline` 僅停用啟動時的額外網路操作，**模型可能仍使用雲端**。

JSONL 僅以 LF 分隔（不能用 splitlines 破壞 Unicode 段落分隔符）。`message_end` 為最終訊息來源，等待 `agent_settled` 且非 aborted；錯誤／未 settle／工具事件不當成成功。只傳最後 assistant text，不傳 thinking、工具輸出、供應商診斷或 stderr。用量只加各 assistant 的 message_end，不再加 agent_end／streaming update，避免重複計數；欄位缺失不以字數補值。

版本不具上述旗標／事件的 pi 會失敗，不能自行放寬隔離規則。後續可評估 SDK／RPC 長駐 adapter、取消與串流，但目前沒有這些功能。

## 後續範圍

1. App 端泛用 TLS 信任／pinning 方案與跨主機真機驗證。
2. pi 圖片：依 provider capability 明確開放並測實際影像傳入。
3. pi coding tools：先定義受限 workspace 與人工批准介面，再加入權限 scope；不能因掃 owner QR 就默默打開所有工具。
4. pi 群聊需全新隔離工作環境及共同可見上下文，不掛入私人 session。
5. 原 Agent Pocket 主機服務尚未遷移；日後需固定版本依賴或受控遷移，避免兩份實作長期漂移。
