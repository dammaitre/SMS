"""Keeps the Store in step with the phone: full sync, notifications, sends, contacts.

`Sync.run()` supervises the connection: it opens a MAP session, syncs, polls,
and when the session is lost (phone out of range, Bluetooth or obexd
restarted) tears down and reconnects with backoff. Messages written while
offline are queued and pushed once the phone is back.
"""

import asyncio
import logging
import uuid
from typing import Callable

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

from .map_client import MapClient, handle_of, parse_timestamp, pull_contacts
from .obex import OBEX, ObexError, call, session_bus
from .phone import normalize
from .store import Store

log = logging.getLogger(__name__)

POLL_SECONDS = 60       # fallback when the phone's new-message events are missed
RETRY = (2, 5, 10, 30, 60)  # reconnect backoff, seconds
ERRORS = (ObexError, asyncio.TimeoutError)


class Sync:
    def __init__(self, store: Store, address: str):
        self.store = store
        self.address = address
        self.bus: MessageBus | None = None
        self.map: MapClient | None = None
        self.connected = False
        self.paths: dict[str, str] = {}  # handle -> Message1 path in the current session
        self._seen_events: set[str] = set()
        self._lost = asyncio.Event()
        self._wake = asyncio.Event()
        self.listeners: list[Callable[[], None]] = []
        self.incoming: list[Callable[[list[str]], None]] = []  # new inbox handles
        self.on_state: list[Callable[[str], None]] = []

    def changed(self):
        for cb in self.listeners:
            cb()

    def state(self, text: str):
        log.info("state: %s", text)
        for cb in self.on_state:
            cb(text)

    # ------------------------------------------------------------ connection

    async def connect(self):
        if not self.bus:
            self.bus = await session_bus()
        self.map = await MapClient.open(self.bus, self.address)
        self.map.on_new = self._on_new
        self.connected = True

    async def _teardown(self):
        self.connected = False
        self.paths.clear()  # Message1 paths die with the session
        if self.map:
            try:
                await asyncio.wait_for(self.map.close(), 3)
            except Exception:
                pass
            self.map = None

    async def close(self):
        await self._teardown()
        if self.bus:
            self.bus.disconnect()
            self.bus = None

    def lose(self, reason: str):
        if self.connected and not self._lost.is_set():
            log.warning("connection lost: %s", reason)
            self._lost.set()

    async def run(self):
        """Connect, sync, and keep reconnecting until cancelled."""
        self.bus = await session_bus()
        await call(self.bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                   "org.freedesktop.DBus", "AddMatch", "s",
                   [f"type='signal',member='NameOwnerChanged',arg0='{OBEX}'"])
        self.bus.add_message_handler(self._on_session_signal)
        sysbus = await self._watch_device()
        attempt = 0
        try:
            while True:
                self._lost.clear()
                self._wake.clear()
                try:
                    self.state("connecting")
                    await self.connect()
                    attempt = 0
                    await self._session()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("session: %s", e)
                finally:
                    await self._teardown()
                    self.changed()
                delay = RETRY[min(attempt, len(RETRY) - 1)]
                attempt += 1
                self.state(f"offline · retry in {delay}s")
                try:  # the phone reconnecting or obexd restarting wakes us early
                    await asyncio.wait_for(self._wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            if sysbus:
                sysbus.disconnect()
            await self.close()

    async def _session(self):
        """One connected session: quick refresh, queued sends, then background work."""
        self.state("refreshing")
        for folder in ("inbox", "sent"):
            await self.refresh(folder, 20)
        self.changed()
        await self.flush_queue()
        background = asyncio.create_task(self._background())
        lost = asyncio.create_task(self._lost.wait())
        try:
            done, _ = await asyncio.wait({background, lost}, return_when=asyncio.FIRST_COMPLETED)
            if background in done:
                background.result()  # re-raise; the loop below never ends normally
        finally:
            background.cancel()
            lost.cancel()

    async def _background(self):
        try:
            self.state("contacts")
            try:
                await self.contacts()
            except ERRORS as e:  # contacts are a nicety; keep going
                log.warning("contacts: %s", e)
            self.state("history")
            await self.full()
            await self.backfill()
            self.state("connected")
            while True:
                await asyncio.sleep(POLL_SECONDS)
                await self.poll()
        except ERRORS as e:
            self.lose(str(e))
            await asyncio.Event().wait()  # let _session see the loss

    def _on_session_signal(self, msg: Message):
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "NameOwnerChanged" and msg.body[0] == OBEX:
            if msg.body[2]:
                self._wake.set()  # obexd (re)started
            else:
                self.lose("obexd stopped")
        elif (msg.member == "InterfacesRemoved" and self.map
              and msg.body[0] == self.map.s.path):
            self.lose("session closed by obexd")

    async def _watch_device(self) -> MessageBus | None:
        """Follow the phone's Connected flag on the system bus (best effort)."""
        suffix = "/dev_" + self.address.replace(":", "_")

        def handler(msg: Message):
            if (msg.message_type == MessageType.SIGNAL and msg.member == "PropertiesChanged"
                    and (msg.path or "").endswith(suffix) and msg.body[0] == "org.bluez.Device1"
                    and "Connected" in msg.body[1]):
                if msg.body[1]["Connected"].value:
                    self._wake.set()
                else:
                    self.lose("phone disconnected")

        try:
            sysbus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            await call(sysbus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                       "org.freedesktop.DBus", "AddMatch", "s",
                       ["type='signal',sender='org.bluez',member='PropertiesChanged',"
                        "path_namespace='/org/bluez'"])
            sysbus.add_message_handler(handler)
            return sysbus
        except Exception as e:
            log.warning("device watch unavailable: %s", e)
            return None

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
        """List every inbox/sent header the phone exposes."""
        count = 0
        for folder in ("inbox", "sent"):
            for h in await self.map.list_all(folder):
                self._ingest(h, folder)
                count += 1
        self.store.reconcile_sent()
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
            if not path or not self.map:
                continue  # not listed in this session; picked up after next full()
            try:
                msg = await self.map.fetch(path)
            except ERRORS as e:
                log.warning("fetch %s: %s", handle, e)
                continue
            self.store.set_body(handle, msg.body)
            done += 1
        if done:
            self.store.reconcile_sent()
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
        log.info("event: new message %s in %s", handle, folder)
        if folder not in ("inbox", "sent"):
            return
        try:
            await self.check(folder, 5)
        except ERRORS as e:
            log.warning("new message %s: %s", handle, e)

    async def check(self, folder: str, n: int = 10) -> list[str]:
        """Pick up messages that arrived since the last look and announce them."""
        new = await self.refresh(folder, n)
        await self.fetch_bodies(handles=new)
        if new and folder == "inbox":
            for cb in self.incoming:
                cb(new)
        self.changed()  # read flags may have changed on the phone too
        return new

    async def poll(self):
        """Safety net for missed notifications; also syncs reads done on the phone."""
        await self.check("inbox")
        await self.check("sent")

    # ------------------------------------------------------------ sending

    async def send(self, address: str, text: str):
        """Queue an SMS; it is pushed now if connected, else on reconnect."""
        local = "local-" + uuid.uuid4().hex
        self.store.add_local(local, address, text.replace("\r\n", "\n"), state="queued")
        self.store.commit()
        self.changed()
        if self.connected:
            await self._push(local)

    async def _push(self, handle: str):
        m = self.store.get(handle)
        if not m or m.state != "queued" or not self.map:
            return
        self.store.set_state(handle, "pending")
        self.store.commit()
        self.changed()
        try:
            await self.map.push(m.address, m.body)
        except ERRORS:
            # Not retried automatically: the phone may have sent it anyway.
            self.store.set_state(handle, "failed")
            self.store.commit()
            self.changed()
            raise
        asyncio.ensure_future(self._settle_sent(m.address))

    async def flush_queue(self):
        for handle in self.store.locals("queued"):
            await self._push(handle)

    async def retry(self, address: str) -> int:
        """Re-queue failed sends of a conversation (Ctrl+R)."""
        failed = self.store.locals("failed", address)
        for handle in failed:
            self.store.set_state(handle, "queued")
        self.store.commit()
        self.changed()
        if self.connected:
            await self.flush_queue()
        return len(failed)

    async def _settle_sent(self, address: str):
        """Replace the local placeholder once the phone files it under 'sent'.

        The new-message notification often ingests the sent copy before we
        look, so match on content rather than on newly seen handles.
        """
        for delay in (2, 5, 15, 30):
            await asyncio.sleep(delay)
            if not self.connected:
                return  # next session's refresh/full() reconciles
            try:
                await self.refresh("sent", 5)
                await self.fetch_bodies(address=address, limit=5)
            except ERRORS:
                continue
            if self.reconcile():
                return

    def reconcile(self) -> int:
        dropped = self.store.reconcile_sent()
        if dropped:
            self.store.commit()
            self.changed()
        return dropped

    # ------------------------------------------------------------ misc

    async def mark_read(self, address: str):
        handles = self.store.mark_read(address)
        self.store.commit()
        self.changed()
        for handle in handles:
            if (path := self.paths.get(handle)) and self.map:
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
