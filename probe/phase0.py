"""Phase 0 feasibility probe: MAP (list / push / notifications) and PBAP via obexd.

Usage:
    .venv/bin/python probe/phase0.py list
    .venv/bin/python probe/phase0.py contacts
    .venv/bin/python probe/phase0.py send +33612345678 "hello from btsms"
    .venv/bin/python probe/phase0.py watch [seconds]

The device defaults to the first paired device exposing a Message Access Server;
override with BTSMS_DEVICE=AA:BB:CC:DD:EE:FF.
"""

import asyncio
import os
import sys
from pathlib import Path

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

OBEX = "org.bluez.obex"
OBEX_PATH = "/org/bluez/obex"
MAS_UUID = "00001132-0000-1000-8000-00805f9b34fb"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "btsms"


class DBus:
    def __init__(self, bus: MessageBus, service: str):
        self.bus = bus
        self.service = service

    async def call(self, path, iface, member, signature="", body=()):
        reply = await self.bus.call(Message(
            destination=self.service, path=path, interface=iface,
            member=member, signature=signature, body=list(body)))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{iface}.{member}: {reply.error_name}: {reply.body}")
        return reply.body

    async def get_all(self, path, iface):
        (props,) = await self.call(path, "org.freedesktop.DBus.Properties",
                                   "GetAll", "s", [iface])
        return {k: v.value for k, v in props.items()}


def unwrap(d):
    return {k: (v.value if isinstance(v, Variant) else v) for k, v in d.items()}


async def find_device():
    if mac := os.environ.get("BTSMS_DEVICE"):
        return mac
    sysbus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    bluez = DBus(sysbus, "org.bluez")
    (objects,) = await bluez.call("/", "org.freedesktop.DBus.ObjectManager",
                                  "GetManagedObjects")
    for path, ifaces in objects.items():
        dev = ifaces.get("org.bluez.Device1")
        if dev and dev["Paired"].value and MAS_UUID in dev["UUIDs"].value:
            print(f"device: {dev['Alias'].value} ({dev['Address'].value})")
            sysbus.disconnect()
            return dev["Address"].value
    sysbus.disconnect()
    sys.exit("no paired device with a Message Access Server")


async def wait_transfer(bus: MessageBus, obex: DBus, path: str, timeout=60):
    """Wait for an org.bluez.obex.Transfer1 to reach complete/error."""
    done = asyncio.get_running_loop().create_future()

    def handler(msg: Message):
        if (msg.message_type == MessageType.SIGNAL and msg.path == path
                and msg.member == "PropertiesChanged"):
            status = msg.body[1].get("Status")
            if status and status.value in ("complete", "error") and not done.done():
                done.set_result(status.value)

    bus.add_message_handler(handler)  # match rule for obexd signals is set in session()
    try:
        # The transfer may already be finished before we got the signal.
        try:
            status = (await obex.get_all(path, "org.bluez.obex.Transfer1"))["Status"]
            if status in ("complete", "error"):
                return status
        except RuntimeError:
            return "complete"  # object already gone => obexd finished it
        return await asyncio.wait_for(done, timeout)
    finally:
        bus.remove_message_handler(handler)


async def session(target: str):
    mac = await find_device()
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    # Receive all obexd signals (transfer progress, new objects).
    await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                           interface="org.freedesktop.DBus", member="AddMatch",
                           signature="s", body=[f"type='signal',sender='{OBEX}'"]))
    obex = DBus(bus, OBEX)
    print(f"opening {target} session (accept the prompt on the phone if asked)...")
    (path,) = await obex.call(OBEX_PATH, "org.bluez.obex.Client1", "CreateSession",
                              "sa{sv}", [mac, {"Target": Variant("s", target)}])
    print(f"session: {path}")
    return bus, obex, path


async def close(bus, obex, path):
    try:
        await obex.call(OBEX_PATH, "org.bluez.obex.Client1", "RemoveSession", "o", [path])
    finally:
        bus.disconnect()


# ---------------------------------------------------------------- MAP

MAP = "org.bluez.obex.MessageAccess1"


async def cmd_list(count=10):
    bus, obex, s = await session("map")
    try:
        await obex.call(s, MAP, "SetFolder", "s", ["telecom/msg"])
        (folders,) = await obex.call(s, MAP, "ListFolders", "a{sv}", [{}])
        print("folders:", [unwrap(f).get("Name") for f in folders])
        for folder in ("inbox", "sent"):
            (msgs,) = await obex.call(s, MAP, "ListMessages", "sa{sv}",
                                      [folder, {"MaxCount": Variant("q", count)}])
            print(f"\n== {folder}: {len(msgs)} message(s)")
            first = None
            for mpath, props in msgs.items():
                p = unwrap(props)
                first = first or mpath
                who = p.get("SenderAddress") if folder == "inbox" else p.get("RecipientAddress")
                print(f"  {p.get('Timestamp')} {p.get('Type'):8} read={p.get('Read')} "
                      f"{who!s:16} {p.get('Subject', '')[:60]!r}")
            if first:
                print(f"  -- raw props of first: {unwrap(msgs[first])}")
                CACHE.mkdir(parents=True, exist_ok=True)
                target = CACHE / "probe-message.bmsg"
                tpath, _ = await obex.call(first, "org.bluez.obex.Message1", "Get",
                                           "sb", [str(target), False])
                status = await wait_transfer(bus, obex, tpath)
                print(f"  -- Get first message: {status}")
                if target.exists():
                    print(target.read_text(errors="replace"))
    finally:
        await close(bus, obex, s)


def build_bmessage(number: str, text: str) -> str:
    msg = f"BEGIN:MSG\r\n{text}\r\nEND:MSG\r\n"
    return "\r\n".join([
        "BEGIN:BMSG", "VERSION:1.0", "STATUS:READ", "TYPE:SMS_GSM",
        "FOLDER:telecom/msg/outbox",
        "BEGIN:VCARD", "VERSION:2.1", "N:", "TEL:", "END:VCARD",
        "BEGIN:BENV",
        "BEGIN:VCARD", "VERSION:2.1", "N:", f"TEL:{number}", "END:VCARD",
        "BEGIN:BBODY", "CHARSET:UTF-8", f"LENGTH:{len(msg.encode())}",
    ]) + "\r\n" + msg + "END:BBODY\r\nEND:BENV\r\nEND:BMSG\r\n"


async def cmd_send(number: str, text: str):
    CACHE.mkdir(parents=True, exist_ok=True)
    src = CACHE / "probe-push.bmsg"
    src.write_bytes(build_bmessage(number, text).encode())
    bus, obex, s = await session("map")
    try:
        await obex.call(s, MAP, "SetFolder", "s", ["telecom/msg"])
        tpath, _ = await obex.call(s, MAP, "PushMessage", "ssa{sv}", [
            str(src), "outbox",
            {"Charset": Variant("s", "utf8"), "Transparent": Variant("b", False),
             "Retry": Variant("b", True)}])
        print(f"push transfer: {tpath}")
        print(f"push result: {await wait_transfer(bus, obex, tpath)}")
    finally:
        await close(bus, obex, s)


async def cmd_watch(seconds=120):
    bus, obex, s = await session("map")

    def handler(msg: Message):
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "InterfacesAdded":
            path, ifaces = msg.body
            if "org.bluez.obex.Message1" in ifaces:
                print(f"NEW MESSAGE {path}: {unwrap(ifaces['org.bluez.obex.Message1'])}")
        elif msg.member == "PropertiesChanged" and "/message" in (msg.path or ""):
            print(f"changed {msg.path}: {unwrap(msg.body[1])}")

    bus.add_message_handler(handler)
    try:
        await obex.call(s, MAP, "SetFolder", "s", ["telecom/msg"])
        print(f"watching {seconds}s - send an SMS to the phone now")
        await asyncio.sleep(seconds)
    finally:
        await close(bus, obex, s)


# ---------------------------------------------------------------- PBAP

async def cmd_contacts():
    bus, obex, s = await session("pbap")
    pbap = "org.bluez.obex.PhonebookAccess1"
    try:
        await obex.call(s, pbap, "Select", "ss", ["int", "pb"])
        (size,) = await obex.call(s, pbap, "GetSize")
        print(f"phonebook size: {size}")
        CACHE.mkdir(parents=True, exist_ok=True)
        target = CACHE / "probe-contacts.vcf"
        tpath, _ = await obex.call(s, pbap, "PullAll", "sa{sv}", [
            str(target), {"Format": Variant("s", "vcard30"),
                          "Fields": Variant("as", ["N", "FN", "TEL"])}])
        print(f"pull: {await wait_transfer(bus, obex, tpath, timeout=120)}")
        text = target.read_text(errors="replace")
        cards = text.count("BEGIN:VCARD")
        print(f"{cards} vCards, {len(text)} bytes -> {target}")
        print("first card:\n" + text.split("END:VCARD")[0] + "END:VCARD")
    finally:
        await close(bus, obex, s)


def main():
    args = sys.argv[1:] or ["list"]
    match args:
        case ["list"]:
            asyncio.run(cmd_list())
        case ["contacts"]:
            asyncio.run(cmd_contacts())
        case ["send", number, text]:
            asyncio.run(cmd_send(number, text))
        case ["watch", *rest]:
            asyncio.run(cmd_watch(int(rest[0]) if rest else 120))
        case _:
            sys.exit(__doc__)


if __name__ == "__main__":
    main()
