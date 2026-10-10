from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS invites(hash TEXT PRIMARY KEY, expires REAL, used INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, hash TEXT UNIQUE, name TEXT, revoked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY, device TEXT REFERENCES devices(id), title TEXT, model TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, chat TEXT REFERENCES chats(id), device TEXT REFERENCES devices(id), text TEXT, state TEXT, reply TEXT DEFAULT '', error TEXT DEFAULT '', created REAL);
        """)
        self.migrate_topics(path)
        from .rooms import Rooms

        if (
            not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='rooms'").fetchone()
            and self.db.execute("SELECT 1 FROM devices").fetchone()
        ):
            backup_path = path.with_name(path.stem + ".before-rooms.sqlite3")
            if not backup_path.exists():
                backup = sqlite3.connect(backup_path)
                try:
                    self.db.backup(backup)
                finally:
                    backup.close()
                backup_path.chmod(0o600)
        self.rooms = Rooms(self)
        self.migrate_performance(path)

    def migrate_performance(self, path):
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
        if "model_snapshot" in columns:
            return
        if self.db.execute("SELECT 1 FROM devices").fetchone():
            backup_path = path.with_name(path.stem + ".before-performance.sqlite3")
            if not backup_path.exists():
                backup = sqlite3.connect(backup_path)
                try:
                    self.db.backup(backup)
                finally:
                    backup.close()
                backup_path.chmod(0o600)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("ALTER TABLE jobs ADD COLUMN model_snapshot TEXT")
            self.db.execute("ALTER TABLE jobs ADD COLUMN performance TEXT")
            self.db.execute("ALTER TABLE room_jobs ADD COLUMN performance TEXT")
            self.db.execute(
                "UPDATE jobs SET model_snapshot=(SELECT model FROM chats WHERE chats.id=jobs.chat) WHERE state IN ('queued','running')"
            )

    @staticmethod
    def job_record(row):
        result = dict(row)
        if "performance" in result:
            result["performance"] = (
                json.loads(result["performance"]) if result["performance"] else None
            )
        return result

    def migrate_topics(self, path: Path):
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(chats)")}
        if "group_id" not in columns and self.db.execute("SELECT 1 FROM chats LIMIT 1").fetchone():
            backup_path = path.with_name(path.stem + ".before-topics.sqlite3")
            if not backup_path.exists():
                backup = sqlite3.connect(backup_path)
                try:
                    self.db.backup(backup)
                finally:
                    backup.close()
                backup_path.chmod(0o600)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS topic_groups(id TEXT PRIMARY KEY, device TEXT REFERENCES devices(id), name TEXT COLLATE NOCASE, created REAL, UNIQUE(device,name))"
            )
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(chats)")}
            if "group_id" not in columns:
                self.db.execute(
                    "ALTER TABLE chats ADD COLUMN group_id TEXT REFERENCES topic_groups(id) ON DELETE SET NULL"
                )
            if "title_manual" not in columns:
                self.db.execute(
                    "ALTER TABLE chats ADD COLUMN title_manual INTEGER NOT NULL DEFAULT 0"
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def topic_name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60:
            raise ValueError("名稱需為 1 至 60 字")
        return name.strip()

    def groups(self, device):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT id,name,created FROM topic_groups WHERE device=? ORDER BY created,id",
                (device,),
            )
        ]

    def group(self, device, gid):
        row = self.db.execute(
            "SELECT id,name,created FROM topic_groups WHERE device=? AND id=?", (device, gid)
        ).fetchone()
        if not row:
            raise KeyError("找不到此群組")
        return dict(row)

    def create_group(self, device, name):
        name = self.topic_name(name)
        gid = str(uuid.uuid4())
        with self.db:
            # Same-name retries return the existing group rather than creating duplicates.
            self.db.execute(
                "INSERT OR IGNORE INTO topic_groups VALUES (?,?,?,?)",
                (gid, device, name, time.time()),
            )
        return dict(
            self.db.execute(
                "SELECT id,name,created FROM topic_groups WHERE device=? AND name=?", (device, name)
            ).fetchone()
        )

    def rename_group(self, device, gid, name):
        self.group(device, gid)
        with self.db:
            self.db.execute(
                "UPDATE topic_groups SET name=? WHERE device=? AND id=?",
                (self.topic_name(name), device, gid),
            )
        return self.group(device, gid)

    def delete_group(self, device, gid):
        self.group(device, gid)
        with self.db:
            self.db.execute(
                "UPDATE chats SET group_id=NULL WHERE device=? AND group_id=?", (device, gid)
            )
            self.db.execute("DELETE FROM topic_groups WHERE device=? AND id=?", (device, gid))

    def update_chat(self, device, cid, patch):
        self.chat(device, cid)
        if not patch or set(patch) - {"title", "group_id", "model"}:
            raise ValueError("不支援的對話欄位")
        if "group_id" in patch and patch["group_id"] is not None:
            self.group(device, patch["group_id"])
        if "model" in patch and (not isinstance(patch["model"], str) or not patch["model"]):
            raise ValueError("模型無效")
        with self.db:
            if "model" in patch:
                self.db.execute(
                    "UPDATE chats SET model=? WHERE device=? AND id=?",
                    (patch["model"], device, cid),
                )
            if "title" in patch:
                self.db.execute(
                    "UPDATE chats SET title=?,title_manual=1 WHERE device=? AND id=?",
                    (self.topic_name(patch["title"]), device, cid),
                )
            if "group_id" in patch:
                self.db.execute(
                    "UPDATE chats SET group_id=? WHERE device=? AND id=?",
                    (patch["group_id"], device, cid),
                )
        return self.chat(device, cid)

    def invite(self, ttl: int = 120, scope: str = "legacy_owner"):
        if scope not in ("legacy_owner", "member"):
            raise ValueError("Invalid scope")
        token = secrets.token_urlsafe(32)
        expires = time.time() + ttl
        with self.db:
            self.db.execute(
                "INSERT INTO invites(hash,expires) VALUES (?,?)", (digest(token), expires)
            )
            self.db.execute("INSERT INTO invite_scopes VALUES(?,?)", (digest(token), scope))
        return token, expires

    def pair(self, invite: str, name: str):
        token, did = secrets.token_urlsafe(32), str(uuid.uuid4())
        with self.db:
            result = self.db.execute(
                "UPDATE invites SET used=1 WHERE hash=? AND used=0 AND expires>?",
                (digest(invite), time.time()),
            )
            if result.rowcount != 1:
                raise ValueError("配對碼已過期或使用，請在 Mac 產生新的配對碼。")
            self.db.execute(
                "INSERT INTO devices(id,hash,name) VALUES (?,?,?)", (did, digest(token), name)
            )
            row = self.db.execute(
                "SELECT scope FROM invite_scopes WHERE hash=?", (digest(invite),)
            ).fetchone()
            self.db.execute(
                "INSERT INTO device_scopes VALUES(?,?)", (did, row[0] if row else "legacy_owner")
            )
        return {"device_id": did, "token": token}

    def device(self, token: str):
        row = self.db.execute(
            "SELECT id FROM devices WHERE hash=? AND revoked=0", (digest(token),)
        ).fetchone()
        return row["id"] if row else None

    def chats(self, device: str):
        return [
            dict(x)
            for x in self.db.execute(
                "SELECT id,title,model,created,group_id FROM chats WHERE device=? ORDER BY created DESC",
                (device,),
            )
        ]

    def chat(self, device: str, cid: str):
        row = self.db.execute(
            "SELECT * FROM chats WHERE id=? AND device=?", (cid, device)
        ).fetchone()
        if not row:
            raise KeyError("找不到此對話")
        return dict(row)

    def create_chat(self, device: str, model: str, group_id: str | None = None):
        if group_id is not None:
            self.group(device, group_id)
        cid = str(uuid.uuid4())
        with self.db:
            self.db.execute(
                "INSERT INTO chats(id,device,title,model,created,group_id) VALUES (?,?,?,?,?,?)",
                (cid, device, "新對話", model, time.time(), group_id),
            )
        return self.chat(device, cid)

    def jobs(self, device: str, cid: str):
        self.chat(device, cid)
        return [
            self.job_record(x)
            for x in self.db.execute(
                "SELECT id,text,state,reply,error,created,model_snapshot,performance FROM jobs WHERE chat=? AND device=? ORDER BY created,rowid",
                (cid, device),
            )
        ]

    def submit(self, device: str, cid: str, jid: str, text: str):
        self.chat(device, cid)
        old = self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if old:
            if old["device"] != device or old["chat"] != cid or old["text"] != text:
                raise ValueError("訊息識別碼衝突")
            return self.job_record(old)
        with self.db:
            busy = self.db.execute(
                "SELECT 1 FROM jobs WHERE chat=? AND state IN ('queued','running')", (cid,)
            ).fetchone()
            if busy:
                raise ValueError("此對話尚有訊息處理中")
            self.db.execute(
                "INSERT INTO jobs(id,chat,device,text,state,created,model_snapshot) VALUES (?,?,?,?,?,?,?)",
                (jid, cid, device, text, "queued", time.time(), self.chat(device, cid)["model"]),
            )
            self.db.execute(
                "UPDATE chats SET title=? WHERE id=? AND title='新對話' AND title_manual=0",
                (text[:36], cid),
            )
        return self.job_record(self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    def recover(self):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='interrupted',error='服務中斷，執行結果未確認。為避免重複操作，不會自動重送。' WHERE state='running'"
            )

    def claim(self):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='failed',error='装置授權已撤銷' WHERE state='queued' AND device IN (SELECT id FROM devices WHERE revoked=1)"
            )
            row = self.db.execute(
                "SELECT j.*,j.model_snapshot AS model FROM jobs j JOIN chats c ON c.id=j.chat WHERE j.state='queued' ORDER BY j.created LIMIT 1"
            ).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (row["id"],))
        return dict(row) if row else None

    def finish(self, jid: str, reply: str = "", error: str = "", performance=None):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state=?,reply=?,error=?,performance=? WHERE id=?",
                (
                    "failed" if error else "completed",
                    reply,
                    error,
                    json.dumps(performance) if performance and not error else None,
                    jid,
                ),
            )


def extract_reply(raw: bytes) -> str:
    data = json.loads(raw)
    if data.get("status") == "error" or data.get("ok") is False:
        raise ValueError("OpenClaw 回報執行失敗")
    inner = data.get("result", data)
    if inner.get("meta", {}).get("error") or inner.get("meta", {}).get("aborted"):
        raise ValueError("OpenClaw 回報執行失敗")
    if isinstance(inner.get("final"), str) and inner["final"].strip():
        return inner["final"].strip()
    payloads = inner.get("payloads", [])
    if any(x.get("isError") for x in payloads):
        raise ValueError("OpenClaw 回報執行失敗")
    reply = "\n".join(x["text"] for x in payloads if isinstance(x.get("text"), str)).strip()
    if not reply:
        raise ValueError("未收到文字回答")
    if len(reply) > 100_000:
        raise ValueError("回答超過第一版文字長度上限")
    return reply
