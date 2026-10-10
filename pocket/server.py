from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from aiohttp import web

from .adapters import create_runner
from .attachments import register as register_attachments
from .core import Store
from .performance import measured
from .room_routes import register
from .rooms import RoomError


def make_app(state: Path, config: dict, runner=None, room_runner=None):
    store = Store(state / "pocket.sqlite3")
    runner = runner or create_runner(config, state)

    @web.middleware
    async def boundary(request, handler):
        try:
            # No browser cross-origin surface, no cookie-based authentication.
            if request.headers.get("Origin"):
                raise web.HTTPForbidden(text="不提供瀏覽器跨來源存取")
            if request.path not in ("/health", "/pair"):
                value = request.headers.get("Authorization", "")
                did = store.device(value[7:]) if value.startswith("Bearer ") else None
                if not did:
                    raise web.HTTPUnauthorized(text="配對已失效，請重新配對。")
                request["device"] = did
                if store.rooms.scope(did) == "member" and request.path.startswith("/chats"):
                    raise RoomError(403, "成員配對沒有私人 Agent 權限")
            return await handler(request)
        except RoomError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except (ValueError, TypeError, json.JSONDecodeError, sqlite3.IntegrityError):
            return web.json_response(
                {"error": "資料無效、名稱重複、對話忙碌或配對碼已過期／使用。"}, status=409
            )
        except KeyError:
            return web.json_response({"error": "找不到此對話"}, status=404)

    app = web.Application(middlewares=[boundary], client_max_size=101 * 1024 * 1024)
    app["store"] = store

    async def body(request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError()
        return data

    def text(data, key, maximum=16_000):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError()
        return value.strip()

    async def health(request):
        return web.json_response({"service": "Agent-pocket", "version": "0.1.0", "backend": config.get("backend", "openclaw"), "protocol_version": 1})

    async def pair(request):
        data = await body(request)
        return web.json_response(store.pair(text(data, "invite", 128), text(data, "name", 80)))

    async def settings(request):
        return web.json_response(
            {
                "agent": config["agent"]
                if store.rooms.scope(request["device"]) != "member"
                else "群聊成員",
                "models": config["models"],
                "default_model": config["default_model"],
                "room_models": config.get("room_models", config["models"]),
                "room_default_model": config.get("room_default_model", config["default_model"]),
                "capabilities": [
                    "topic_groups",
                    "shared_rooms_v1",
                    "attachments_v1",
                    "chat_model_v1",
                    "performance_v1",
                ]
                + (["room_agent_v1"] if room_runner else []),
                "private_chat": store.rooms.scope(request["device"]) != "member",
            }
        )

    async def chats(request):
        if request.method == "GET":
            return web.json_response(store.chats(request["device"]))
        data = await body(request)
        if "group_id" in data and not isinstance(data["group_id"], (str, type(None))):
            raise ValueError()
        model = data.get("model", config["default_model"])
        if model not in config["models"]:
            raise ValueError()
        return web.json_response(
            store.create_chat(request["device"], model, data.get("group_id") or None), status=201
        )

    async def groups(request):
        did = request["device"]
        gid = request.match_info.get("gid")
        if request.method == "GET":
            return web.json_response(store.groups(did))
        if request.method == "DELETE":
            store.delete_group(did, gid)
            return web.json_response({"removed": True})
        data = await body(request)
        if request.method == "PATCH":
            return web.json_response(store.rename_group(did, gid, text(data, "name", 60)))
        return web.json_response(store.create_group(did, text(data, "name", 60)), status=201)

    async def edit_chat(request):
        data = await body(request)
        if "model" in data and data["model"] not in config["models"]:
            raise ValueError("未授權的模型")
        if "group_id" in data:
            if not isinstance(data["group_id"], (str, type(None))):
                raise ValueError()
            data["group_id"] = data["group_id"] or None
        return web.json_response(
            store.update_chat(request["device"], request.match_info["cid"], data)
        )

    async def messages(request):
        did, cid = request["device"], request.match_info["cid"]
        if request.method == "GET":
            return web.json_response(store.jobs(did, cid))
        data = await body(request)
        jid = str(uuid.UUID(text(data, "id", 36)))
        result = store.submit(did, cid, jid, text(data, "text"))
        return web.json_response(result, status=202)

    async def unpair(request):
        store.rooms.revoke_device(request["device"])
        return web.json_response({"revoked": True})

    async def worker():
        while True:
            job = store.claim()
            if not job:
                await asyncio.sleep(0.2)
                continue
            started = time.monotonic()
            try:
                reply = await runner(files.attach_job(job))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not return raw CLI stderr or credentials to clients.
                message = (
                    str(exc)
                    if isinstance(exc, (ValueError, RuntimeError))
                    else "服務執行失敗，未自動重送。"
                )
                store.finish(job["id"], error=message[:250])
            else:
                store.finish(
                    job["id"], reply=reply, performance=measured(reply, time.monotonic() - started)
                )

    async def lifetime(app):
        store.recover()
        store.rooms.recover()
        task = asyncio.create_task(worker())
        room_task = asyncio.create_task(room_worker())
        yield
        room_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await room_task
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        store.recover()
        store.rooms.recover()
        store.db.close()

    async def room_worker():
        while True:
            job = store.rooms.claim() if room_runner else None
            if not job:
                await asyncio.sleep(0.3)
                continue
            started = time.monotonic()
            try:
                reply = await room_runner(files.attach_job(job, room=True))
            except asyncio.CancelledError:
                raise
            except Exception:
                store.rooms.finish(job, error="群聊 Agent 執行失敗，未自動重送")
            else:
                store.rooms.finish(
                    job, reply=reply, performance=measured(reply, time.monotonic() - started)
                )

    def room_ready(rid):
        row = store.db.execute("SELECT model FROM rooms WHERE id=?", (rid,)).fetchone()
        return (
            room_runner is not None
            and row is not None
            and row["model"] != "none"
            and row["model"] in config.get("room_models", config["models"])
        )

    files = register_attachments(app, store, state, room_ready, config)
    register(app, store, config, room_ready)
    app.cleanup_ctx.append(lifetime)
    app.router.add_get("/health", health)
    app.router.add_post("/pair", pair)
    app.router.add_get("/settings", settings)
    app.router.add_get("/groups", groups)
    app.router.add_post("/groups", groups)
    app.router.add_patch("/groups/{gid}", groups)
    app.router.add_delete("/groups/{gid}", groups)
    app.router.add_patch("/chats/{cid}", edit_chat)
    app.router.add_get("/chats", chats)
    app.router.add_post("/chats", chats)
    app.router.add_get("/chats/{cid}/messages", messages)
    app.router.add_post("/chats/{cid}/messages", messages)
    app.router.add_post("/unpair", unpair)
    return app
