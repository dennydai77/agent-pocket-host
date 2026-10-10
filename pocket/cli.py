from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
import shutil
import ssl
import subprocess
import webbrowser
from pathlib import Path

import segno
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .core import Store
from .server import make_app


def valid_host(host):
    address = ipaddress.ip_address(host)
    if address.version != 4 or not (
        address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")
    ):
        raise ValueError("僅允許 IPv4 loopback 或 Tailscale 位址")
    return address


def initialize(state: Path, host: str, port: int, agent: str, backend="openclaw", model=None):
    address = valid_host(host)
    if (state / "config.json").exists():
        raise ValueError("已有設定，不覆蓋既有主機身分。")
    if backend not in ("openclaw", "pi"):
        raise ValueError("不支援的 backend")
    if not 1 <= port <= 65535:
        raise ValueError("port 必須介於 1–65535")
    executable = shutil.which(backend)
    if not executable:
        raise ValueError(f"找不到 {backend}")
    if backend == "openclaw":
        catalog = json.loads(
            subprocess.check_output([executable, "models", "list", "--json"], timeout=60)
        )
        allowed = [
            m["key"]
            for m in catalog["models"]
            if m.get("available") and not m.get("local") and "configured" in m.get("tags", [])
        ]
    else:
        output = subprocess.check_output(
            [
                executable,
                "--offline",
                "--no-extensions",
                "--no-mcp",
                "--no-skills",
                "--no-context-files",
                "--list-models",
            ],
            timeout=60,
            text=True,
        )
        allowed = []
        for line in output.split("\n"):
            fields = line.split()
            if len(fields) >= 6 and fields[0] != "provider" and fields[-1] in ("yes", "no"):
                allowed.append(fields[0] + "/" + fields[1])
    if not allowed:
        raise ValueError("沒有可選模型；請先在原後端完成設定")
    default = model or ("openai/gpt-5.6-sol" if "openai/gpt-5.6-sol" in allowed else allowed[0])
    if default not in allowed:
        raise ValueError("指定模型不在後端清單內")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Agent-pocket")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(address)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    for name, data in [
        (
            "key.pem",
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        ),
        ("cert.pem", cert.public_bytes(serialization.Encoding.PEM)),
    ]:
        (state / name).write_bytes(data)
        (state / name).chmod(0o600)
    config = {
        "host": host,
        "port": port,
        "origin": f"https://{host}:{port}",
        "agent": agent,
        "backend": backend,
        backend: executable,
        "default_model": default,
        "models": allowed,
        "certificate_sha256": hashlib.sha256(
            cert.public_bytes(serialization.Encoding.DER)
        ).hexdigest(),
    }
    (state / "config.json").write_text(json.dumps(config, indent=2))
    Store(state / "pocket.sqlite3").db.close()
    return config


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Agent-pocket 私有手機連線服務")
    parser.add_argument("--state", type=Path, default=Path(".runtime/host"))
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--host", required=True)
    init.add_argument("--port", type=int, default=8944)
    init.add_argument("--backend", choices=["openclaw", "pi"], default="openclaw")
    init.add_argument("--model", help="目前後端已設定的 provider/model")
    init.add_argument("--agent", default="main")
    serve = sub.add_parser("serve")
    serve.add_argument("--room-agent", choices=["off", "local"], default="off")
    invite = sub.add_parser("invite")
    invite.add_argument("--open", action="store_true")
    invite.add_argument("--scope", choices=["legacy_owner", "member"], default="legacy_owner")
    sub.add_parser("devices")
    sub.add_parser("status")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("device_id")
    args = parser.parse_args()
    state = args.state.resolve()
    if args.command == "init":
        initialize(state, args.host, args.port, args.agent, args.backend, args.model)
        print("主機設定已建立；尚未啟動服務。")
        return
    config = json.loads((state / "config.json").read_text())
    if args.command == "serve":
        valid_host(config["host"])
        lock = (state / "server.lock").open("w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(state / "cert.pem", state / "key.pem")
        room_runner = None
        config = dict(config)
        if config.get("backend", "openclaw") == "openclaw":
            catalog = json.loads(
                subprocess.check_output(
                    [config["openclaw"], "models", "list", "--json"], timeout=60
                )
            )
            config["vision_models"] = [
                m["key"] for m in catalog["models"] if "image" in m.get("input", "")
            ]
        else:
            config["vision_models"] = []
            if args.room_agent != "off":
                raise ValueError("pi backend 尚未提供群聊 Agent；不回退 OpenClaw")

        config["room_models"] = ["none"]
        config["room_default_model"] = "none"
        if args.room_agent == "local":
            from .local_room_agent import MODEL, LocalRoomAgent

            room_runner = LocalRoomAgent(config["openclaw"], state)
            asyncio.run(room_runner.preflight())
            config["room_models"].append(MODEL)
        web.run_app(
            make_app(state, config, room_runner=room_runner),
            host=config["host"],
            port=config["port"],
            ssl_context=context,
            access_log=None,
        )
    else:
        store = Store(state / "pocket.sqlite3")
        if args.command == "invite":
            token, expires = store.invite(scope=args.scope)
            payload = {
                "version": 1,
                "scope": args.scope,
                "origin": config["origin"],
                "certificate_sha256": config["certificate_sha256"],
                "invite": token,
                "expires": expires,
            }
            # QR is a short-lived secret: write private files, never print the payload.
            value = json.dumps(payload, separators=(",", ":"))
            path = state / "pairing.png"
            segno.make(value).save(path, scale=6)
            (state / "pairing.json").write_text(value)
            print("已建立有效 120 秒的一次性 QR：", path)
            if args.open:
                webbrowser.open(path.as_uri())
        elif args.command == "status":
            import urllib.request

            context = ssl.create_default_context(cafile=str(state / "cert.pem"))
            try:
                with urllib.request.urlopen(
                    config["origin"] + "/health", context=context, timeout=5
                ) as response:
                    health = json.load(response)
            except Exception:
                health = {"reachable": False}
            print(
                json.dumps(
                    {
                        "backend": config.get("backend", "openclaw"),
                        "origin": config["origin"],
                        "health": health,
                        "default_model": config["default_model"],
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "devices":
            print(
                json.dumps(
                    [dict(r) for r in store.db.execute("SELECT id,name,revoked FROM devices")],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "revoke":
            store.rooms.revoke_device(args.device_id)
            print("已處理撤銷裝置要求")
        store.db.close()


if __name__ == "__main__":
    main()
