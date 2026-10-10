# Agent Pocket wire protocol v1

HTTPS only；iOS 驗證服務憑證 SHA256 與 SSL host／有效期。Host 只綁 IPv4 loopback 或 100.64.0.0/10；VPN 可連線不等於有授權。

QR JSON 欄位：version=1、scope=legacy_owner 或 member、origin、certificate_sha256、invite、expires。invite 為短效秘密，不應放公開 README 範例、issue、截圖或 Git；配對票據只以 hash 保存、成功交換一次即失效。

- `POST /pair`：invite/name → device_id/token。之後使用 Authorization: Bearer，不用 cookie。
- `GET /health`：service/version/backend/protocol_version，無秘密。
- `GET /settings`：可用 models、capabilities、private_chat。主機設定模型清單不是所有 provider 都已實測。
- `GET/POST /chats`、`PATCH /chats/{id}`：私人對話；model 改選只影響下一次 submit。
- `GET/POST /chats/{id}/messages`：持久 job、UUID 冪等、model_snapshot、performance。
- `POST /unpair`：撤銷呼叫裝置。
- `/groups`、`/v1/rooms` 與 attachments 端點沿用抽取的原服務實作及測試。

performance 的 input_tokens 包含已回報快取；output_tokens 依供應商用量，可能含推理與多次呼叫。elapsed_seconds 是 worker wall time，包含載入／工具／傳輸，不含佇列及手機同步，不是純 decode 速度。

附件上傳100 MiB、模型解析20 MiB；上傳不等於授權分析。pi 初版 vision_models 空，不接受 image analysis。配對碼不含供應商 API key，手機不直接取得 OpenClaw／pi 的認證檔。
