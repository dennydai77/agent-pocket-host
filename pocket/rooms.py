"""Shared rooms. Device identities are server-issued, never names supplied in a message."""

import hashlib
import json
import secrets
import time
import uuid


class RoomError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def uid(value):
    return str(uuid.UUID(value))


def title(value, maximum=60):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RoomError(400, "文字為空或超過長度限制")
    return value.strip()


class Rooms:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS device_scopes(device TEXT PRIMARY KEY REFERENCES devices(id), scope TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS invite_scopes(hash TEXT PRIMARY KEY, scope TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rooms(id TEXT PRIMARY KEY, owner TEXT REFERENCES devices(id), title TEXT NOT NULL, model TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active', epoch INTEGER NOT NULL DEFAULT 1, seq INTEGER NOT NULL DEFAULT 0, client_id TEXT, created REAL, UNIQUE(owner,client_id));
        CREATE TABLE IF NOT EXISTS room_members(room TEXT REFERENCES rooms(id), device TEXT REFERENCES devices(id), role TEXT NOT NULL, status TEXT NOT NULL, visible_from INTEGER NOT NULL, PRIMARY KEY(room,device));
        CREATE TABLE IF NOT EXISTS room_invites(id TEXT PRIMARY KEY, hash TEXT UNIQUE, room TEXT REFERENCES rooms(id), issuer TEXT REFERENCES devices(id), expires REAL, used_by TEXT, revoked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS room_messages(id TEXT PRIMARY KEY, room TEXT REFERENCES rooms(id), seq INTEGER, author TEXT, kind TEXT, text TEXT, reply_to TEXT, client_id TEXT, payload TEXT, source_from INTEGER, created REAL, UNIQUE(room,seq), UNIQUE(room,author,client_id));
        CREATE TABLE IF NOT EXISTS room_jobs(id TEXT PRIMARY KEY, room TEXT REFERENCES rooms(id), trigger_id TEXT UNIQUE REFERENCES room_messages(id), requester TEXT REFERENCES devices(id), state TEXT, model TEXT, epoch INTEGER, context_start INTEGER, context_end INTEGER, reply_id TEXT, error TEXT DEFAULT '', created REAL);
        CREATE TABLE IF NOT EXISTS room_placements(room TEXT REFERENCES rooms(id), device TEXT REFERENCES devices(id), group_id TEXT REFERENCES topic_groups(id) ON DELETE SET NULL, PRIMARY KEY(room,device));
        CREATE TABLE IF NOT EXISTS room_reads(room TEXT, device TEXT, seq INTEGER, PRIMARY KEY(room,device));
        CREATE TABLE IF NOT EXISTS room_audit(id INTEGER PRIMARY KEY,room TEXT,actor TEXT,action TEXT,target TEXT,created REAL);
        COMMIT;
        """)

    def scope(self, did):
        row = self.db.execute("SELECT scope FROM device_scopes WHERE device=?", (did,)).fetchone()
        return row["scope"] if row else "legacy_owner"

    def auth(self, did, rid, owner=False, active=False):
        r = self.db.execute(
            """SELECT r.*,m.role,m.visible_from FROM rooms r JOIN room_members m ON m.room=r.id JOIN devices d ON d.id=m.device WHERE r.id=? AND m.device=? AND m.status='active' AND d.revoked=0""",
            (rid, did),
        ).fetchone()
        if not r:
            raise RoomError(404, "聊天室不存在或已無存取權")
        if owner and r["owner"] != did:
            raise RoomError(403, "只有建立者可以管理聊天室")
        if active and r["state"] != "active":
            raise RoomError(409, "聊天室已封存")
        return dict(r)

    def audit(self, did, rid, action, target=""):
        self.db.execute(
            "INSERT INTO room_audit(room,actor,action,target,created) VALUES(?,?,?,?,?)",
            (rid, did, action, target, time.time()),
        )

    def listing(self, did):
        ids = self.db.execute(
            "SELECT room FROM room_members WHERE device=? AND status='active'", (did,)
        ).fetchall()
        result = []
        for row in ids:
            r = self.auth(did, row["room"])
            r["member_count"] = self.db.execute(
                "SELECT count(*) FROM room_members m JOIN devices d ON d.id=m.device WHERE room=? AND status='active' AND d.revoked=0",
                (r["id"],),
            ).fetchone()[0]
            placement = self.db.execute(
                "SELECT group_id FROM room_placements WHERE room=? AND device=?", (r["id"], did)
            ).fetchone()
            r["group_id"] = placement[0] if placement else None
            read = self.db.execute(
                "SELECT seq FROM room_reads WHERE room=? AND device=?", (r["id"], did)
            ).fetchone()
            r["unread"] = self.db.execute(
                "SELECT count(*) FROM room_messages WHERE room=? AND seq>? AND seq>=? AND source_from>=? AND author!=?",
                (r["id"], read[0] if read else 0, r["visible_from"], r["visible_from"], did),
            ).fetchone()[0]
            result.append(r)
        return result

    def create(self, did, data, models):
        name = title(data.get("title"))
        cid = uid(data["client_id"])
        model = data["model"]
        if model not in models:
            raise RoomError(400, "模型不在主機清單中")
        with self.db:
            old = self.db.execute(
                "SELECT * FROM rooms WHERE owner=? AND client_id=?", (did, cid)
            ).fetchone()
            if old:
                if old["title"] != name or old["model"] != model:
                    raise RoomError(409, "建立要求內容衝突")
                return self.auth(did, old["id"])
            rid = str(uuid.uuid4())
            self.db.execute(
                "INSERT INTO rooms(id,owner,title,model,client_id,created) VALUES(?,?,?,?,?,?)",
                (rid, did, name, model, cid, time.time()),
            )
            self.db.execute("INSERT INTO room_members VALUES(?,?, 'owner','active',1)", (rid, did))
            self.audit(did, rid, "create")
        return self.auth(did, rid)

    def update(self, did, rid, data, models):
        with self.db:
            self.auth(did, rid, owner=True, active=True)
            if not data or set(data) - {"title", "model", "state"}:
                raise RoomError(400, "不支援的欄位")
            for key, val in data.items():
                if key == "title":
                    val = title(val)
                elif key == "model" and val not in models:
                    raise RoomError(400, "模型不支援")
                elif key == "state" and val != "archived":
                    raise RoomError(400, "僅允許封存")
                self.db.execute(f"UPDATE rooms SET {key}=?,epoch=epoch+1 WHERE id=?", (val, rid))
            self.audit(did, rid, "update")
        return self.auth(did, rid)

    def invite(self, did, rid):
        with self.db:
            self.auth(did, rid, owner=True, active=True)
            token = secrets.token_urlsafe(32)
            iid = str(uuid.uuid4())
            expires = time.time() + 120
            self.db.execute(
                "INSERT INTO room_invites(id,hash,room,issuer,expires) VALUES(?,?,?,?,?)",
                (iid, hashlib.sha256(token.encode()).hexdigest(), rid, did, expires),
            )
            self.audit(did, rid, "invite", iid)
        return {"id": iid, "token": token, "expires": expires}

    def invitation(self, did, token, accept=False):
        if not isinstance(token, str):
            raise RoomError(400, "邀請格式錯誤")
        with self.db:
            i = self.db.execute(
                "SELECT * FROM room_invites WHERE hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if not i or i["revoked"] or (i["used_by"] and i["used_by"] != did):
                raise RoomError(409, "邀請已失效")
            if i["used_by"] == did:
                return self.auth(did, i["room"])
            if i["expires"] <= time.time():
                raise RoomError(409, "邀請已過期")
            r = self.auth(i["issuer"], i["room"], owner=True, active=True)
            if accept:
                existing = self.db.execute(
                    "SELECT status FROM room_members WHERE room=? AND device=?", (r["id"], did)
                ).fetchone()
                if not existing or existing["status"] != "active":
                    self.db.execute(
                        "INSERT INTO room_members VALUES(?,?,'member','active',?) ON CONFLICT(room,device) DO UPDATE SET status='active',role='member',visible_from=excluded.visible_from",
                        (r["id"], did, r["seq"] + 1),
                    )
                    self.db.execute("UPDATE rooms SET epoch=epoch+1 WHERE id=?", (r["id"],))
                self.db.execute("UPDATE room_invites SET used_by=? WHERE id=?", (did, i["id"]))
                self.audit(did, r["id"], "join")
                return self.auth(did, r["id"])
            owner = self.db.execute(
                "SELECT name FROM devices WHERE id=?", (r["owner"],)
            ).fetchone()[0]
            return {
                "id": r["id"],
                "title": r["title"],
                "owner_name": owner,
                "expires": i["expires"],
                "history": "加入後訊息",
            }

    def members(self, did, rid):
        self.auth(did, rid)
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT d.id,d.name,m.role,m.visible_from FROM room_members m JOIN devices d ON d.id=m.device WHERE room=? AND status='active' AND d.revoked=0",
                (rid,),
            )
        ]

    def membership(self, did, rid, target, history=None):
        with self.db:
            r = self.auth(did, rid, owner=(did != target or history is not None), active=True)
            m = self.db.execute(
                "SELECT * FROM room_members WHERE room=? AND device=? AND status='active'",
                (rid, target),
            ).fetchone()
            if not m:
                raise RoomError(404, "找不到成員")
            if history is None:
                if target == r["owner"]:
                    raise RoomError(409, "建立者請先封存聊天室，不能直接退出")
                self.db.execute(
                    "UPDATE room_members SET status='removed' WHERE room=? AND device=?",
                    (rid, target),
                )
            else:
                value = int(history)
                if not 1 <= value <= m["visible_from"]:
                    raise RoomError(400, "只能明確擴大歷史範圍")
                self.db.execute(
                    "UPDATE room_members SET visible_from=? WHERE room=? AND device=?",
                    (value, rid, target),
                )
            self.db.execute("UPDATE rooms SET epoch=epoch+1 WHERE id=?", (rid,))
            self.audit(did, rid, "history" if history is not None else "remove", target)
        return {"ok": True}

    def message(self, did, rid, mid):
        r = self.auth(did, rid)
        m = self.db.execute(
            "SELECT * FROM room_messages WHERE room=? AND id=? AND seq>=? AND source_from>=?",
            (rid, mid, r["visible_from"], r["visible_from"]),
        ).fetchone()
        if not m:
            raise RoomError(404, "訊息不存在或不可見")
        return dict(m)

    def messages(self, did, rid, after=0, limit=50):
        r = self.auth(did, rid)
        return [
            dict(m)
            for m in self.db.execute(
                """SELECT m.id,m.seq,m.author,COALESCE(d.name,'Agent') AS author_name,m.kind,m.text,m.reply_to,m.created,m.source_from FROM room_messages m LEFT JOIN devices d ON d.id=m.author WHERE m.room=? AND m.seq>? AND m.seq>=? AND m.source_from>=? ORDER BY m.seq LIMIT ?""",
                (rid, max(0, after), r["visible_from"], r["visible_from"], min(max(limit, 1), 100)),
            )
        ]

    def append(self, rid, author, kind, text, reply=None, client=None, payload=None, source=None):
        seq = self.db.execute(
            "UPDATE rooms SET seq=seq+1 WHERE id=? RETURNING seq", (rid,)
        ).fetchone()[0]
        mid = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO room_messages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (mid, rid, seq, author, kind, text, reply, client, payload, source or seq, time.time()),
        )
        return mid

    def send(self, did, rid, data, agent_ready=False):
        if set(data) - {"client_id", "text", "reply_to_id", "ask_agent"}:
            raise RoomError(400, "不接受作者或執行權限覆寫")
        cid = uid(data["client_id"])
        text = title(data.get("text"), 16000)
        reply = data.get("reply_to_id") or None
        ask = data.get("ask_agent", False)
        if ask not in (True, False, "true", "false"):
            raise RoomError(400, "ask_agent 格式錯誤")
        ask = ask is True or ask == "true"
        payload = json.dumps([text, reply, ask], ensure_ascii=False)
        with self.db:
            self.auth(did, rid, active=True)
            old = self.db.execute(
                "SELECT * FROM room_messages WHERE room=? AND author=? AND client_id=?",
                (rid, did, cid),
            ).fetchone()
            if old:
                if old["payload"] != payload:
                    raise RoomError(409, "訊息識別碼內容衝突")
                return self.message(did, rid, old["id"])
            if ask and not agent_ready:
                raise RoomError(503, "群聊 Agent 尚未就緒，未送出訊息")
            source = None
            if reply:
                source = self.message(did, rid, reply)["source_from"]
            mid = self.append(rid, did, "human", text, reply, cid, payload, source)
            if ask:
                self.enqueue(did, rid, mid)
        return self.message(did, rid, mid)

    def enqueue(self, did, rid, mid):
        r = self.auth(did, rid, active=True)
        m = self.message(did, rid, mid)
        old = self.db.execute("SELECT * FROM room_jobs WHERE trigger_id=?", (mid,)).fetchone()
        if old:
            return dict(old)
        if (
            self.db.execute(
                "SELECT count(*) FROM room_jobs WHERE state IN ('queued','running')"
            ).fetchone()[0]
            >= 32
        ):
            raise RoomError(429, "Agent 佇列已滿，稍後再試")
        start = self.db.execute(
            "SELECT max(visible_from) FROM room_members m JOIN devices d ON d.id=m.device WHERE room=? AND status='active' AND d.revoked=0",
            (rid,),
        ).fetchone()[0]
        if m["seq"] < start or m["source_from"] < start:
            raise RoomError(409, "此訊息不在全體成員共同可見範圍，請重新提問")
        jid = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO room_jobs(id,room,trigger_id,requester,state,model,epoch,context_start,context_end,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (jid, rid, mid, did, "queued", r["model"], r["epoch"], start, r["seq"], time.time()),
        )
        return dict(self.db.execute("SELECT * FROM room_jobs WHERE id=?", (jid,)).fetchone())

    def jobs(self, did, rid):
        r = self.auth(did, rid)
        return [
            self.store.job_record(j)
            for j in self.db.execute(
                "SELECT id,trigger_id,state,reply_id,error,performance FROM room_jobs WHERE room=? AND context_start>=? ORDER BY created",
                (rid, r["visible_from"]),
            )
        ]

    def claim(self):
        with self.db:
            rows = self.db.execute(
                "SELECT * FROM room_jobs WHERE state='queued' ORDER BY created"
            ).fetchall()
            for row in rows:
                j = dict(row)
                try:
                    r = self.auth(j["requester"], j["room"], active=True)
                except RoomError:
                    r = None
                if not r or r["epoch"] != j["epoch"]:
                    self.db.execute(
                        "UPDATE room_jobs SET state='superseded',error='成員或權限已改變，請重新提問' WHERE id=?",
                        (j["id"],),
                    )
                    continue
                self.db.execute("UPDATE room_jobs SET state='running' WHERE id=?", (j["id"],))
                msgs = [
                    dict(m)
                    for m in self.db.execute(
                        "SELECT id,seq,author,kind,text,reply_to FROM room_messages WHERE room=? AND seq BETWEEN ? AND ? AND source_from>=? ORDER BY seq DESC LIMIT 100",
                        (j["room"], j["context_start"], j["context_end"], j["context_start"]),
                    )
                ][::-1]
                # Explicit bounded context; no reused model history.
                while len(json.dumps(msgs, ensure_ascii=False)) > 48000 and len(msgs) > 1:
                    msgs.pop(0)
                j["messages"] = msgs
                j["trigger_message"] = self.message(j["requester"], j["room"], j["trigger_id"])
                return j
        return None

    def finish(self, j, reply="", error="", performance=None):
        with self.db:
            try:
                r = self.auth(j["requester"], j["room"], active=True)
            except RoomError:
                r = None
            if not r or r["epoch"] != j["epoch"]:
                state = "superseded"
                error = "成員或權限已改變，舊回答不發布"
                mid = None
            elif error:
                state = "failed"
                mid = None
            else:
                state = "completed"
                mid = self.append(
                    j["room"],
                    "agent",
                    "agent",
                    title(reply, 100000),
                    j["trigger_id"],
                    source=j["context_start"],
                )
            self.db.execute(
                "UPDATE room_jobs SET state=?,error=?,reply_id=?,performance=? WHERE id=?",
                (
                    state,
                    error,
                    mid,
                    json.dumps(performance) if performance and state == "completed" else None,
                    j["id"],
                ),
            )

    def revoke_device(self, did):
        with self.db:
            self.db.execute("UPDATE devices SET revoked=1 WHERE id=?", (did,))
            self.db.execute(
                "UPDATE rooms SET epoch=epoch+1 WHERE id IN (SELECT room FROM room_members WHERE device=? AND status='active')",
                (did,),
            )

    def recover(self):
        with self.db:
            self.db.execute(
                "UPDATE room_jobs SET state='interrupted',error='服務中斷，未自動重送' WHERE state='running'"
            )
