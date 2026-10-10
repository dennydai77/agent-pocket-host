# Agent Pocket Host development

- This is the standalone host repository, not the iOS app. Keep runtime, session, database, certificates, keys and pairing QR out of Git.
- The original Agent Pocket production host is independent: do not stop, migrate, or change it as part of work here without a request to do so.
- Run `uv run --frozen pytest tests -q` for backend changes; use synthetic data and independent loopback ports for integration proof.
- Pairing and authorization are host responsibilities, never model decisions. Adapters cannot bypass device/room scopes or silently fall back to another backend.
- Pi v1 is private text-only/no tools. Broader tools or group-agent support requires an explicit design and tests, not just removing CLI flags.
- Document actual test evidence and missing real-device coverage, especially cross-host iOS TLS compatibility.
