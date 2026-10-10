# 驗證紀錄 — 2026-10-10

版本 0.1.0；macOS、Python 3.14.4、pi CLI 1.1.0。

- 38 項後端測試通過，包含既有授權／附件／群聊與新增 pi adapter 測試。
- Python 核心程式 Ruff 通過；uv lock --check 通過；sdist 與 wheel 建置成功，wheel 已檢查無 runtime 資料。
- repository-owned Skill quick_validate 通過，wrapper help 可正常執行。
- 獨立 loopback TLS 測試：配對、建立私人對話、兩輪 pi / Ollama gemma4:12b 回答與前文延續、撤銷後拒絕存取，全部成功。
- 兩輪實測耗時約 9.99 秒、17.78 秒；回傳 input/output token 分別為 768/35、912/136。用量包含模型計量的輸出，不等同畫面字數。
- QR PNG 390×390 與協定欄位已驗證；state 目錄權限 0700。未將測試配對資料納入版本控制。
- 測試服務結束已關閉，測試裝置已撤銷。沒有新增 daemon、搬移正式資料或替換原 Agent Pocket 服務。

## 尚未驗收

- 其他 Mac／Linux 上的安裝與真實手機跨主機配對。
- 原 App 的其他主機自簽 TLS 限制尚未修改。
- pi 圖片分析、工具操作及群聊 Agent 尚未實作；目前為私人文字後端。
- OpenClaw adapter 沿用原實作並通過回歸測試；本次未重新進行雲端模型實測。
- Skill 已放在 repo，未安裝至任何現有 Agent。尚未建立或推送公開 GitHub，也尚未決定開源授權。
