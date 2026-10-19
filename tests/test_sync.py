import asyncio
import unittest

from btsms import sync as sync_mod
from btsms.obex import ObexError
from btsms.store import Store
from btsms.sync import Sync

ADDR = "+33600000001"


class FakeMap:
    def __init__(self):
        self.pushed: list[tuple[str, str]] = []
        self.fail = False

    async def push(self, number, text):
        if self.fail:
            raise ObexError("PushMessage: org.bluez.obex.Error.Failed")
        self.pushed.append((number, text))

    async def list(self, folder, n=10, offset=0):
        return []

    async def close(self):
        pass


class TestSending(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        original = sync_mod.Sync._settle_sent
        sync_mod.Sync._settle_sent = lambda self, address: asyncio.sleep(0)
        self.addCleanup(setattr, sync_mod.Sync, "_settle_sent", original)
        self.store = Store(":memory:")
        self.sync = Sync(self.store, "AA:BB:CC:DD:EE:FF")
        self.fake = FakeMap()

    def states(self):
        return [m.state for m in self.store.messages(ADDR)]

    async def test_offline_send_is_queued_then_flushed(self):
        await self.sync.send(ADDR, "un")
        await self.sync.send(ADDR, "deux")
        self.assertEqual(self.states(), ["queued", "queued"])
        self.sync.map, self.sync.connected = self.fake, True  # phone is back
        await self.sync.flush_queue()
        self.assertEqual(self.fake.pushed, [(ADDR, "un"), (ADDR, "deux")])
        self.assertEqual(self.states(), ["pending", "pending"])

    async def test_failed_push_is_kept_and_retried(self):
        self.sync.map, self.sync.connected = self.fake, True
        self.fake.fail = True
        with self.assertRaises(ObexError):
            await self.sync.send(ADDR, "yo")
        self.assertEqual(self.states(), ["failed"])
        await self.sync.flush_queue()  # failed sends are not retried on their own
        self.assertEqual(self.fake.pushed, [])
        self.fake.fail = False
        self.assertEqual(await self.sync.retry(ADDR), 1)
        self.assertEqual(self.fake.pushed, [(ADDR, "yo")])
        self.assertEqual(self.states(), ["pending"])

    async def test_failed_but_actually_sent_is_reconciled(self):
        self.sync.map, self.sync.connected = self.fake, True
        self.fake.fail = True
        with self.assertRaises(ObexError):
            await self.sync.send(ADDR, "yo")
        ts = self.store.messages(ADDR)[0].ts
        self.store.upsert_header("9", "sent", ADDR, "", ts + 3, "yo", True, True)
        self.assertEqual(self.sync.reconcile(), 1)
        self.assertEqual([m.handle for m in self.store.messages(ADDR)], ["9"])

    async def test_lose_only_when_connected(self):
        self.sync.lose("x")
        self.assertFalse(self.sync._lost.is_set())
        self.sync.connected = True
        self.sync.lose("x")
        self.assertTrue(self.sync._lost.is_set())


if __name__ == "__main__":
    unittest.main()
