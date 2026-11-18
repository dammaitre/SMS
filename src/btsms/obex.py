"""Thin asyncio wrapper over obexd's D-Bus API (session bus, org.bluez.obex)."""

import asyncio
import os
from pathlib import Path

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

OBEX = "org.bluez.obex"
OBEX_PATH = "/org/bluez/obex"
CLIENT = "org.bluez.obex.Client1"
TRANSFER = "org.bluez.obex.Transfer1"
PROPS = "org.freedesktop.DBus.Properties"
MAS_UUID = "00001132-0000-1000-8000-00805f9b34fb"

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "btsms"


class ObexError(RuntimeError):
    pass


def unwrap(d: dict) -> dict:
    return {k: (v.value if isinstance(v, Variant) else v) for k, v in d.items()}


async def call(bus: MessageBus, dest, path, iface, member, signature="", body=()):
    reply = await bus.call(Message(destination=dest, path=path, interface=iface,
                                   member=member, signature=signature, body=list(body)))
    if reply.message_type == MessageType.ERROR:
        raise ObexError(f"{member}: {reply.error_name}: {' '.join(map(str, reply.body))}")
    return reply.body


async def find_device() -> tuple[str, str]:
    """(address, alias) of $BTSMS_DEVICE or the first paired device with MAP."""
    want = os.environ.get("BTSMS_DEVICE", "").upper()
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        (objects,) = await call(bus, "org.bluez", "/", "org.freedesktop.DBus.ObjectManager",
                                "GetManagedObjects")
    finally:
        bus.disconnect()
    for ifaces in objects.values():
        dev = ifaces.get("org.bluez.Device1")
        if not dev:
            continue
        addr = dev["Address"].value
        if (want and addr == want) or (not want and dev["Paired"].value
                                       and MAS_UUID in dev["UUIDs"].value):
            return addr, dev["Alias"].value
    raise ObexError("no paired device with a Message Access Server")


class Session:
    """One obexd client session (map or pbap) on a shared session bus."""

    def __init__(self, bus: MessageBus, path: str):
        self.bus = bus
        self.path = path
        self._transfers: dict[str, asyncio.Future] = {}
        bus.add_message_handler(self._on_signal)

    @classmethod
    async def open(cls, bus: MessageBus, address: str, target: str) -> "Session":
        (path,) = await call(bus, OBEX, OBEX_PATH, CLIENT, "CreateSession", "sa{sv}",
                             [address, {"Target": Variant("s", target)}])
        return cls(bus, path)

    async def close(self):
        self.bus.remove_message_handler(self._on_signal)
        try:
            await call(self.bus, OBEX, OBEX_PATH, CLIENT, "RemoveSession", "o", [self.path])
        except ObexError:
            pass  # already gone (phone disconnected)

    def call(self, path, iface, member, signature="", body=()):
        return call(self.bus, OBEX, path, iface, member, signature, body)

    async def set_property(self, path, iface, name, variant: Variant):
        await self.call(path, PROPS, "Set", "ssv", [iface, name, variant])

    def _on_signal(self, msg: Message):
        if (msg.message_type == MessageType.SIGNAL and msg.member == "PropertiesChanged"
                and msg.path in self._transfers):
            status = msg.body[1].get("Status")
            fut = self._transfers[msg.path]
            if status and status.value in ("complete", "error") and not fut.done():
                fut.set_result(status.value)

    async def wait_transfer(self, path: str, timeout: float = 60) -> None:
        """Wait for a Transfer1 to finish; raise ObexError if it failed."""
        fut = self._transfers[path] = asyncio.get_running_loop().create_future()
        try:
            try:
                (status,) = await self.call(path, PROPS, "Get", "ss", [TRANSFER, "Status"])
                if status.value in ("complete", "error"):
                    fut.set_result(status.value)
            except ObexError:
                fut.set_result("complete")  # object already removed => finished
            if await asyncio.wait_for(fut, timeout) == "error":
                raise ObexError(f"transfer {path} failed")
        finally:
            del self._transfers[path]


async def session_bus() -> MessageBus:
    """Session bus subscribed to every obexd signal."""
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    await call(bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
               "AddMatch", "s", [f"type='signal',sender='{OBEX}'"])
    return bus
