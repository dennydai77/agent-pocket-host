import asyncio
import io
import uuid

import pytest
from PIL import Image

from pocket.attachments import extract
from pocket.server import make_app


@pytest.fixture
async def fixture(tmp_path, aiohttp_client):
    seen = []

    async def run(job):
        seen.append(job)
        return "analysis complete"

    cfg = {
        "agent": "main",
        "models": ["cloud"],
        "default_model": "cloud",
        "vision_models": ["cloud"],
        "room_models": ["ollama/gemma4:12b"],
    }
    app = make_app(tmp_path, cfg, runner=run, room_runner=run)
    client = await aiohttp_client(app)
    store = app["store"]
    a = store.pair(store.invite()[0], "A")
    b = store.pair(store.invite(scope="member")[0], "B")

    def auth(user):
        return {"Authorization": "Bearer " + user["token"]}

    return client, store, a, b, auth, seen, tmp_path


async def test_private_upload_download_idempotency_and_explicit_analysis(fixture):
    client, s, a, b, auth, seen, root = fixture
    chat = s.create_chat(a["device_id"], "cloud")
    aid = str(uuid.uuid4())
    base = "/v1/files/chats/" + chat["id"]
    path = base + "/" + aid + "?name=notes.txt"
    r = await client.put(path, headers=auth(a), data=b"ATTACHMENT_ONLY_CANARY")
    assert r.status == 201, await r.text()
    await r.json()
    assert not seen
    assert (await client.put(path, headers=auth(a), data=b"ATTACHMENT_ONLY_CANARY")).status == 200
    assert (await client.put(path, headers=auth(a), data=b"different")).status == 409
    assert len(await (await client.get(base, headers=auth(a))).json()) == 1
    download = "/v1/attachments/" + aid + "/download"
    assert (await client.get(download, headers=auth(b))).status == 403
    assert (await client.get(download)).status == 401
    assert await (await client.get(download, headers=auth(a))).read() == b"ATTACHMENT_ONLY_CANARY"
    data = {"id": str(uuid.uuid4()), "question": "summarize"}
    analyze = "/v1/attachments/" + aid + "/analyze"
    assert (await client.post(analyze, headers=auth(a), json=data)).status == 202
    assert (await client.post(analyze, headers=auth(a), json=data)).status == 200
    for _ in range(30):
        if seen:
            break
        await asyncio.sleep(0.05)
    assert len(seen) == 1 and "ATTACHMENT_ONLY_CANARY" in seen[0]["text"]
    assert s.jobs(a["device_id"], chat["id"])[0]["state"] == "completed"
    assert not list((root / "attachments").glob("*.upload"))


async def test_room_history_membership_and_analysis_scope(fixture):
    c, s, a, b, auth, seen, root = fixture
    r = s.rooms.create(
        a["device_id"],
        {"client_id": str(uuid.uuid4()), "title": "room", "model": "ollama/gemma4:12b"},
        ["ollama/gemma4:12b"],
    )
    rid = r["id"]
    aid = str(uuid.uuid4())
    base = "/v1/files/rooms/" + rid
    assert (
        await c.put(base + "/" + aid + "?name=old.txt", headers=auth(a), data=b"old history")
    ).status == 201
    invite = s.rooms.invite(a["device_id"], rid)
    s.rooms.invitation(b["device_id"], invite["token"], accept=True)
    assert await (await c.get(base, headers=auth(b))).json() == []
    assert (await c.get("/v1/attachments/" + aid + "/download", headers=auth(b))).status == 404
    assert (
        await c.post(
            "/v1/attachments/" + aid + "/analyze", headers=auth(a), json={"id": str(uuid.uuid4())}
        )
    ).status == 409
    new = str(uuid.uuid4())
    assert (
        await c.put(base + "/" + new + "?name=new.txt", headers=auth(a), data=b"common information")
    ).status == 201
    assert len(await (await c.get(base, headers=auth(b))).json()) == 1
    result = await c.post(
        "/v1/attachments/" + new + "/analyze", headers=auth(b), json={"id": str(uuid.uuid4())}
    )
    assert result.status == 202, await result.text()
    s.rooms.membership(a["device_id"], rid, b["device_id"])
    assert (await c.get("/v1/attachments/" + new + "/download", headers=auth(b))).status == 404
    assert not seen  # queued room job invalidated by membership change


async def test_images_and_unsupported_file(fixture):
    c, s, a, b, auth, seen, root = fixture
    chat = s.create_chat(a["device_id"], "cloud")
    base = "/v1/files/chats/" + chat["id"]
    image = io.BytesIO()
    Image.new("RGB", (128, 128), "red").save(image, format="PNG")
    aid = str(uuid.uuid4())
    assert (
        await c.put(base + "/" + aid + "?name=red.png", headers=auth(a), data=image.getvalue())
    ).status == 201
    assert (
        await c.post(
            "/v1/attachments/" + aid + "/analyze", headers=auth(a), json={"id": str(uuid.uuid4())}
        )
    ).status == 202
    for _ in range(30):
        if seen:
            break
        await asyncio.sleep(0.05)
    assert len(seen) == 1 and seen[0]["attachment_image"].endswith(".jpg")
    bad = str(uuid.uuid4())
    assert (
        await c.put(base + "/" + bad + "?name=archive.zip", headers=auth(a), data=b"opaque file")
    ).status == 201
    assert (
        await c.post(
            "/v1/attachments/" + bad + "/analyze", headers=auth(a), json={"id": str(uuid.uuid4())}
        )
    ).status == 400


def test_extract_limits(tmp_path):
    path = tmp_path / "doc"
    path.write_text("x" * 20000)
    assert extract(path, "test.txt") == ("text", "x" * 12000, True)
    path.write_bytes(b"\x89PNGnot an image")
    with pytest.raises(Exception):
        extract(path, "image.png")


def test_pdf_text_and_blank(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=400)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 300 Td (PDF_PROOF) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "file"
    writer.write(path)
    kind, text, truncated = extract(path, "test.pdf")
    assert kind == "pdf" and "PDF_PROOF" in text and not truncated


async def test_concurrent_analysis_and_size_limit(fixture):
    c, s, a, b, auth, seen, root = fixture
    chat = s.create_chat(a["device_id"], "cloud")
    aid = str(uuid.uuid4())
    path = "/v1/files/chats/" + chat["id"] + "/" + aid + "?name=notes.txt"
    assert (await c.put(path, headers=auth(a), data=b"concurrent request")).status == 201
    body = {"id": str(uuid.uuid4()), "question": "summarize"}
    responses = await asyncio.gather(
        *[
            c.post("/v1/attachments/" + aid + "/analyze", headers=auth(a), json=body)
            for _ in range(2)
        ]
    )
    assert sorted(r.status for r in responses) == [200, 202]
    too_big = await c.put(
        "/v1/files/chats/" + chat["id"] + "/" + str(uuid.uuid4()) + "?name=big.bin",
        headers=auth(a),
        data=b"x" * (100 * 1024 * 1024 + 1),
    )
    assert too_big.status == 413


async def test_large_file_transfer_separate_from_analysis(fixture):
    c, s, a, b, auth, seen, root = fixture
    chat = s.create_chat(a["device_id"], "cloud")
    aid = str(uuid.uuid4())
    payload = b"x" * (21 * 1024 * 1024)
    response = await c.put(
        "/v1/files/chats/" + chat["id"] + "/" + aid + "?name=large.txt",
        headers=auth(a),
        data=payload,
    )
    assert response.status == 201
    assert (
        await (await c.get("/v1/attachments/" + aid + "/download", headers=auth(a))).read()
        == payload
    )
    assert (
        await c.post(
            "/v1/attachments/" + aid + "/analyze", headers=auth(a), json={"id": str(uuid.uuid4())}
        )
    ).status == 413
    assert not seen
