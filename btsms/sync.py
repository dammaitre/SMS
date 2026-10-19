"""Keeps the Store in step with the phone: full sync, notifications, sends, contacts."""

import asyncio
import logging
import uuid
from typing import Callable

from dbus_fast.aio import MessageBus

from .map_client import MapClient, handle_of, parse_timestamp, pull_contacts
from .obex import ObexError, session_bus
from .phone import normalize
from .store import Store

log = logging.getLogger(__name__)


class Sync:
    def __init__(self, store: Store, address: str):
        self.store = store
        self.address = address
        self.bus: MessageBus | None = None
        self.map: MapClient | None = None
        self.paths: dict[str, str] = {}  # handle -> Message1 path in the current session
        self._seen_events: set[str] = set()
        self.listeners: list[Callable[[], None]] = []

    def changed(self):
        for cb in self.listeners:
            cb()

    async def connect(self):
        self.bus = await session_bus()
        self.map = await MapClient.open(self.bus, self.address)
        self.map.on_new = self._on_new

    async def close(self):
        if self.map:
            await self.map.close()
        if self.bus:
            self.bus.disconnect()

    # ------------------------------------------------------------ headers

    def _ingest(self, h: dict, folder: str) -> bool:
        """Store one listed header; True if it was new."""
        outgoing = folder != "inbox"
        handle = h["handle"]
        self.paths[handle] = h["path"]
        new = not self.store.has(handle)
        self.store.upsert_header(
            handle, folder,
            normalize(h.get("RecipientAddress" if outgoing else "SenderAddress", "")),
            h.get("Recipient" if outgoing else "Sender", ""),
            parse_timestamp(h.get("Timestamp", "")),
            h.get("Subject", ""), h.get("Read", False), outgoing)
        return new

    async def full(self) -> int:
        """List every inbox/sent header (cheap: one transfer per 500 messages)."""
        count = 0
        for folder in ("inbox", "sent"):
            for h in await self.map.list_all(folder):
                self._ingest(h, folder)
                count += 1
        self.store.commit()
        self.changed()
        return count

    async def refresh(self, folder: str, n: int = 10) -> list[str]:
        new = [h["handle"] for h in await self.map.list(folder, n) if self._ingest(h, folder)]
        self.store.commit()
        return new

    # ------------------------------------------------------------ bodies

    async def fetch_bodies(self, address: str | None = None, limit: int = 50,
                           handles: list[str] | None = None) -> int:
        """Get full text for messages whose listing subject may be lossy."""
        done = 0
        for handle in handles if handles is not None else self.store.incomplete(address, limit):
            path = self.paths.get(handle)
            if not path:
                continue  # not listed in this session; picked up after next full()
            try:
                msg = await self.map.fetch(path)
            except (ObexError, asyncio.TimeoutError) as e:
                log.warning("fetch %s: %s", handle, e)
                continue
            self.store.set_body(handle, msg.body)
            done += 1
        if done:
            self.store.commit()
            self.changed()
        return done

    async def backfill(self, batch: int = 50):
        """Fetch every remaining body, newest first, in small batches."""
        while self.store.incomplete(limit=1):
            if not await self.fetch_bodies(limit=batch):
                return

    # ------------------------------------------------------------ events

    async def _on_new(self, path: str, props: dict):
        handle = handle_of(path)
        if handle in self._seen_events:
            return  # obexd reports the same event twice
        self._seen_events.add(handle)
        folder = props.get("Folder", "/telecom/msg/inbox").rsplit("/", 1)[-1] or "inbox"
        if folder not in ("inbox", "sent"):
            return
        try:
            new = await self.refresh(folder, 5)
            await self.fetch_bodies(handles=new)
        except ObexError as e:
            log.warning("new message %s: %s", handle, e)
        self.changed()

    # ------------------------------------------------------------ actions

    async def send(self, address: str, text: str):
        text = text.replace("\r\n", "\n")
        local = "local-" + uuid.uuid4().hex
        self.store.add_local(local, address, text)
        self.store.commit()
        self.changed()
        try:
            await self.map.push(address, text)
        except (ObexError, asyncio.TimeoutError):
            self.store.set_state(local, "failed")
            self.store.commit()
            self.changed()
            raise
        asyncio.ensure_future(self._settle_sent(address, text))

    async def _settle_sent(self, address: str, text: str):
        """Replace the local placeholder once the phone files it under 'sent'."""
        for delay in (2, 5, 15):
            await asyncio.sleep(delay)
            try:
                new = await self.refresh("sent", 5)
                await self.fetch_bodies(handles=new)
            except ObexError:
                continue
            if new:
                self.store.drop_local_matching(address, text)
                self.store.commit()
                self.changed()
                return

    async def mark_read(self, address: str):
        handles = self.store.mark_read(address)
        self.store.commit()
        self.changed()
        for handle in handles:
            if path := self.paths.get(handle):
                try:
                    await self.map.mark_read(path)
                except ObexError as e:
                    log.warning("mark read %s: %s", handle, e)

    async def contacts(self) -> int:
        cards = await pull_contacts(self.bus, self.address)
        pairs = {normalize(t): c["name"] for c in cards if c["name"] for t in c["tels"]}
        self.store.set_contacts(list(pairs.items()))
        self.store.commit()
        self.changed()
        return len(pairs)
