"""MAP (MessageAccess1) and PBAP (PhonebookAccess1) clients on top of obex.Session."""

import asyncio
import uuid
from datetime import datetime
from typing import Awaitable, Callable

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from . import bmessage
from .obex import CACHE, Session, unwrap

MAP = "org.bluez.obex.MessageAccess1"
MESSAGE = "org.bluez.obex.Message1"
PBAP = "org.bluez.obex.PhonebookAccess1"


def handle_of(path: str) -> str:
    return path.rsplit("/message", 1)[1]


def parse_timestamp(ts: str) -> float:
    """MAP 'YYYYMMDDTHHMMSS[Z|±hhmm]' -> unix time (phone local time if no zone)."""
    if not ts:
        return 0.0
    if len(ts) > 15:
        zone = "+0000" if ts[15:] == "Z" else ts[15:]
        return datetime.strptime(ts[:15] + zone, "%Y%m%dT%H%M%S%z").timestamp()
    return datetime.strptime(ts[:15], "%Y%m%dT%H%M%S").timestamp()


class MapClient:
    def __init__(self, session: Session):
        self.s = session
        self.on_new: Callable[[str, dict], Awaitable[None] | None] | None = None
        session.bus.add_message_handler(self._on_signal)

    @classmethod
    async def open(cls, bus: MessageBus, address: str) -> "MapClient":
        client = cls(await Session.open(bus, address, "map"))
        await client.s.call(client.s.path, MAP, "SetFolder", "s", ["telecom/msg"])
        return client

    async def close(self):
        self.s.bus.remove_message_handler(self._on_signal)
        await self.s.close()

    async def list(self, folder: str, max_count: int = 100, offset: int = 0) -> list[dict]:
        """Message headers, newest first. Each dict has 'path' and 'handle' added."""
        (msgs,) = await self.s.call(self.s.path, MAP, "ListMessages", "sa{sv}", [folder, {
            "MaxCount": Variant("q", max_count), "Offset": Variant("q", offset),
            "SubjectLength": Variant("y", 255)}])
        return [unwrap(p) | {"path": path, "handle": handle_of(path)}
                for path, p in msgs.items()]

    async def list_all(self, folder: str, page: int = 500) -> list[dict]:
        # Galaxy S23: only the newest 100 per folder are exposed (Offset >= 100
        # returns nothing) and 100 headers take ~5 s, so older history only
        # exists in our local cache.
        out, offset = [], 0
        while True:
            batch = await self.list(folder, page, offset)
            out += batch
            if len(batch) < page:
                return out
            offset += page

    async def fetch(self, path: str) -> bmessage.BMessage:
        """Full message content; the path must have been listed in this session."""
        CACHE.mkdir(parents=True, exist_ok=True)
        target = CACHE / f"get-{uuid.uuid4().hex}.bmsg"
        try:
            transfer, _ = await self.s.call(path, MESSAGE, "Get", "sb", [str(target), False])
            await self.s.wait_transfer(transfer)
            return bmessage.parse(target.read_bytes().decode("utf-8", "replace"))
        finally:
            target.unlink(missing_ok=True)

    async def push(self, number: str, text: str) -> None:
        """Hand an SMS to the phone. Returns once the phone accepted it."""
        CACHE.mkdir(parents=True, exist_ok=True)
        src = CACHE / f"push-{uuid.uuid4().hex}.bmsg"
        src.write_bytes(bmessage.build(number, text))
        try:
            transfer, _ = await self.s.call(self.s.path, MAP, "PushMessage", "ssa{sv}", [
                str(src), "outbox", {"Charset": Variant("s", "utf8"),
                                     "Transparent": Variant("b", False),
                                     "Retry": Variant("b", True)}])
            await self.s.wait_transfer(transfer)
        finally:
            src.unlink(missing_ok=True)

    async def mark_read(self, path: str) -> None:
        await self.s.set_property(path, MESSAGE, "Read", Variant("b", True))

    def _on_signal(self, msg: Message):
        # New-message events: obexd adds a stub Message1 (Status=notification,
        # no sender/subject), sometimes twice for the same handle. Objects
        # created by our own ListMessages are announced the same way but carry
        # Status=complete, so they are ignored.
        if (self.on_new and msg.message_type == MessageType.SIGNAL
                and msg.member == "InterfacesAdded"):
            path, ifaces = msg.body
            props = unwrap(ifaces.get(MESSAGE, {}))
            if (path.startswith(self.s.path + "/message")
                    and props.get("Status") == "notification"):
                result = self.on_new(path, props)
                if result is not None:
                    asyncio.ensure_future(result)


async def pull_contacts(bus: MessageBus, address: str) -> list[dict]:
    """Phone's internal phonebook -> [{"name", "tels"}]."""
    s = await Session.open(bus, address, "pbap")
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / "contacts.vcf"
    try:
        await s.call(s.path, PBAP, "Select", "ss", ["int", "pb"])
        transfer, _ = await s.call(s.path, PBAP, "PullAll", "sa{sv}", [
            str(target), {"Format": Variant("s", "vcard30"),
                          "Fields": Variant("as", ["N", "FN", "TEL"])}])
        await s.wait_transfer(transfer, timeout=120)
        return bmessage.parse_vcards(target.read_bytes().decode("utf-8", "replace"))
    finally:
        target.unlink(missing_ok=True)
        await s.close()
