"""btsms - SMS from the terminal through a Bluetooth-paired Android phone.

    python -m btsms          interactive UI
    python -m btsms sync     sync messages + contacts, print conversations

Log: ~/.cache/btsms/btsms.log
"""

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import sys
import time

from .obex import CACHE, find_device
from .store import Store
from .sync import Sync


async def cmd_sync(fetch: int = 50):
    address, alias = await find_device()
    print(f"{alias} ({address})")
    sync = Sync(Store(), address)
    await sync.connect()
    try:
        t = time.monotonic()
        n = await sync.full()
        print(f"headers: {n} in {time.monotonic() - t:.1f}s")
        t = time.monotonic()
        print(f"contacts: {await sync.contacts()} numbers in {time.monotonic() - t:.1f}s")
        t = time.monotonic()
        done = await sync.fetch_bodies(limit=fetch)
        print(f"bodies: {done} fetched in {time.monotonic() - t:.1f}s")
    finally:
        await sync.close()
    for c in sync.store.conversations()[:15]:
        when = time.strftime("%d/%m %H:%M", time.localtime(c.last_ts))
        unread = f" ({c.unread})" if c.unread else ""
        print(f"  {when}  {c.title[:24]:24}{unread:5} {c.last_body[:50]!r}")


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(CACHE / "btsms.log", maxBytes=1_000_000, backupCount=2)
    logging.basicConfig(handlers=[handler], level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    match sys.argv[1:]:
        case []:
            from .ui import BtSms
            BtSms().run()
        case ["sync"]:
            asyncio.run(cmd_sync())
        case _:
            sys.exit(__doc__)


if __name__ == "__main__":
    main()
