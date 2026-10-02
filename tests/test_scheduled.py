"""Additional acceptance checks; all Telegram calls end at a fake client."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from telethon.errors import MediaCaptionTooLongError
from telethon.tl.functions.messages import EditMessageRequest
from telethon.tl.types import InputPeerChannel

import slop_writer.publish as publish
import slop_writer.scheduled as scheduled
import slop_writer.server as boundary
from .conftest import run
from .factories import msg, photo


def call(server, name, **arguments):
    blocks = run(server.call_tool(name, arguments))
    return "\n".join(block.text for block in blocks)


def payload_of(error):
    text = str(error)
    return json.loads(text[text.index("{"):])


class Client:
    def __init__(self, messages):
        self.messages = messages
        self.requests = []
        self.refuse = False

    async def __call__(self, request):
        ids = getattr(request, "id", None)
        return SimpleNamespace(messages=[m for m in self.messages if ids is None or m.id in ids])

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        self.requests.append(request)
        if self.refuse:
            raise MediaCaptionTooLongError(request=request)
        # Scheduled edits return None, which the production code must tolerate.
        return None

    async def edit_message(self, peer, message_id, text=None, **kwargs):
        return await self._call(None, EditMessageRequest(
            peer=peer, id=message_id, message=text,
            entities=kwargs.get("formatting_entities"),
            schedule_date=kwargs.get("schedule"),
        ))


def wire(monkeypatch, tmp_path, messages):
    client = Client(messages)
    @asynccontextmanager
    async def session(*args):
        yield client, InputPeerChannel(1001, 1)
    monkeypatch.setattr(publish, "channel_session", session)
    monkeypatch.setattr(scheduled, "channel_session", session)
    monkeypatch.setattr(boundary, "require_session", lambda *args: None)
    return boundary.build_server(tmp_path), client


@pytest.mark.parametrize("above", [False, True])
def test_reschedule_success_preserves_body_and_caption_position(monkeypatch, tmp_path, above):
    existing = msg(7, text="Исходный текст 😀", media=photo(), date=datetime.now(UTC)+timedelta(hours=2))
    existing.invert_media = above
    server, client = wire(monkeypatch, tmp_path, [existing])
    when = datetime.now(UTC)+timedelta(hours=3)
    reply = call(server, "publish_reschedule", channel="@demo", message_id=7, at=when.isoformat())
    request, = client.requests
    assert request.id == 7 and request.schedule_date == when
    assert request.message is None and request.entities is None
    assert request.invert_media is above
    assert "Rescheduled" in reply
    assert "_call" not in client.__dict__


@pytest.mark.parametrize("above", [False, True])
def test_edit_success_keeps_imminent_time_and_caption_position(monkeypatch, tmp_path, above):
    when = datetime.now(UTC)+timedelta(minutes=5)
    existing = msg(7, text="Старый текст", media=photo(), date=when)
    existing.invert_media = above
    server, client = wire(monkeypatch, tmp_path, [existing])
    reply = call(server, "publish_edit", channel="@demo", message_id=7, body="😀 **Новый текст**")
    request, = client.requests
    assert request.id == 7 and request.schedule_date == when
    assert request.message == "😀 Новый текст"
    assert len(request.entities) == 1
    assert request.entities[0].offset == 3 and request.entities[0].length == 11
    assert request.invert_media is above
    assert "Edited" in reply
    assert "_call" not in client.__dict__


@pytest.mark.parametrize("operation", ["publish_edit", "publish_reschedule"])
def test_stale_queue_id_has_actionable_error(monkeypatch, tmp_path, operation):
    server, client = wire(monkeypatch, tmp_path, [])
    arguments = {"body": "replacement"} if operation == "publish_edit" else {
        "at": (datetime.now(UTC)+timedelta(hours=2)).isoformat()
    }
    with pytest.raises(ToolError) as exc:
        call(server, operation, channel="@demo", message_id=7, **arguments)
    assert payload_of(exc.value)["code"] == "NO_SUCH_MESSAGE"
    assert client.requests == []


def test_queue_lists_every_post_and_groups_album(monkeypatch, tmp_path):
    when = datetime.now(UTC)+timedelta(hours=2)
    messages = [msg(1, text="first", date=when),
                msg(2, grouped_id=900, media=photo(), date=when),
                msg(3, text="album caption", grouped_id=900, media=photo(), date=when),
                msg(4, text="last", date=when)]
    _, client = wire(monkeypatch, tmp_path, messages)
    queue = run(scheduled.list_scheduled("demo", "unused"))
    assert [item["id"] for item in queue.items] == [1, 3, 4]
    assert queue.items[1]["text"] == "album caption"
    assert len(queue.items[1]["attachments"]) == 2
    assert client.requests == []


def test_rejected_caption_is_reported_and_dispatch_patch_restored(monkeypatch, tmp_path):
    existing = msg(7, text="old", media=photo(), date=datetime.now(UTC)+timedelta(hours=2))
    existing.invert_media = True
    server, client = wire(monkeypatch, tmp_path, [existing])
    client.refuse = True
    with pytest.raises(ToolError) as exc:
        call(server, "publish_edit", channel="@demo", message_id=7, body="replacement")
    assert payload_of(exc.value)["code"] == "MESSAGE_TOO_LONG"
    assert "_call" not in client.__dict__
