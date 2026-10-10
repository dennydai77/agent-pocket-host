import json

from aiohttp import web

from .rooms import RoomError


def register(app, store, config, ready):
    rooms = store.rooms

    async def route(req):
        did = req["device"]
        rid = req.match_info.get("rid")
        action = req.match_info.get("action")
        data = await req.json() if req.can_read_body else {}
        if not isinstance(data, dict):
            raise RoomError(400, "請提供 JSON 物件")
        if req.path == "/v1/rooms":
            result = (
                rooms.listing(did)
                if req.method == "GET"
                else rooms.create(did, data, config.get("room_models", config["models"]))
            )
        elif req.path.startswith("/v1/room-invites/"):
            result = rooms.invitation(did, data.get("token"), accept=req.path.endswith("/accept"))
        elif action == "messages":
            result = (
                rooms.messages(
                    did, rid, int(req.query.get("after", 0)), int(req.query.get("limit", 50))
                )
                if req.method == "GET"
                else rooms.send(did, rid, data, ready(rid))
            )
        elif action == "jobs":
            result = rooms.jobs(did, rid)
        elif action == "ask-agent":
            if not ready(rid):
                raise RoomError(503, "群聊 Agent 尚未就緒")
            with rooms.db:
                result = rooms.enqueue(did, rid, data["message_id"])
        elif action == "invites":
            result = rooms.invite(did, rid)
            result["qr"] = json.dumps(
                {
                    "version": 1,
                    "type": "pocket-room-invite-v1",
                    "origin": config["origin"],
                    "certificate_sha256": config["certificate_sha256"],
                    "invite": result["token"],
                    "expires": result["expires"],
                },
                separators=(",", ":"),
            )
        elif action == "members":
            result = rooms.members(did, rid)
        elif action == "leave":
            result = rooms.membership(did, rid, did)
        elif action == "placement":
            rooms.auth(did, rid)
            gid = data.get("group_id") or None
            if gid:
                store.group(did, gid)
            with rooms.db:
                rooms.db.execute(
                    "INSERT INTO room_placements VALUES(?,?,?) ON CONFLICT(room,device) DO UPDATE SET group_id=excluded.group_id",
                    (rid, did, gid),
                )
            result = {"ok": True}
        elif action == "read":
            r = rooms.auth(did, rid)
            seq = min(max(0, int(data["seq"])), r["seq"])
            with rooms.db:
                rooms.db.execute(
                    "INSERT INTO room_reads VALUES(?,?,?) ON CONFLICT(room,device) DO UPDATE SET seq=max(seq,excluded.seq)",
                    (rid, did, seq),
                )
            result = {"ok": True}
        elif req.match_info.get("pid"):
            result = rooms.membership(
                did,
                rid,
                req.match_info["pid"],
                data.get("visible_from_seq") if req.method == "PATCH" else None,
            )
        elif req.match_info.get("iid"):
            rooms.auth(did, rid, owner=True)
            with rooms.db:
                rooms.db.execute(
                    "UPDATE room_invites SET revoked=1 WHERE id=? AND room=?",
                    (req.match_info["iid"], rid),
                )
            result = {"ok": True}
        else:
            result = (
                rooms.auth(did, rid)
                if req.method == "GET"
                else rooms.update(did, rid, data, config.get("room_models", config["models"]))
            )
        return web.json_response(result)

    app.router.add_get("/v1/rooms", route)
    app.router.add_post("/v1/rooms", route)
    for action in ("preview", "accept"):
        app.router.add_post("/v1/room-invites/" + action, route)
    app.router.add_get("/v1/rooms/{rid}", route)
    app.router.add_patch("/v1/rooms/{rid}", route)
    for action in ("messages", "members", "jobs"):
        app.router.add_get("/v1/rooms/{rid}/{action:" + action + "}", route)
    for action in ("messages", "invites", "ask-agent", "leave"):
        app.router.add_post("/v1/rooms/{rid}/{action:" + action + "}", route)
    for action in ("placement", "read"):
        app.router.add_patch("/v1/rooms/{rid}/{action:" + action + "}", route)
    app.router.add_delete("/v1/rooms/{rid}/members/{pid}", route)
    app.router.add_patch("/v1/rooms/{rid}/members/{pid}", route)
    app.router.add_delete("/v1/rooms/{rid}/invites/{iid}", route)
