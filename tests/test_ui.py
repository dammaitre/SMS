import time
import unittest

from btsms.store import Store
from btsms.ui import BtSms, NewConversation


def fake_store() -> Store:
    s = Store(":memory:")
    now = time.time()
    s.upsert_header("1", "inbox", "+33600000001", "Alice", now - 3600, "Salut ! Tu viens ce soir ?", True, False)
    s.upsert_header("2", "sent", "+33600000001", "Alice", now - 3500, "Oui, 10 min 🚀", True, True)
    s.upsert_header("3", "inbox", "+33600000001", "Alice", now - 60, "Super", False, False)
    s.upsert_header("4", "inbox", "AIRBNB", "AIRBNB", now - 86400 * 2, "Votre code est 123456.", False, False)
    s.upsert_header("5", "inbox", "+33600000002", "", now - 86400, "Bob ici", True, False)
    s.set_contacts([("+33600000001", "Alice Martin"), ("+33600000002", "Bob Durand"),
                    ("+33600000003", "Charlie Petit")])
    s.commit()
    return s


class TestUI(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_and_new(self):
        app = BtSms(fake_store(), connect=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.addresses, ["+33600000001", "+33600000002", "AIRBNB"])
            self.assertEqual(app.current, "+33600000001")
            self.assertEqual(app.focused.id, "compose")
            await pilot.press("ctrl+down")
            self.assertEqual(app.current, "+33600000002")
            await pilot.press("ctrl+down", "ctrl+down")  # clamps at the end
            self.assertEqual(app.current, "AIRBNB")
            await pilot.press("ctrl+up")
            self.assertEqual(app.current, "+33600000002")
            await pilot.press("alt+up")
            self.assertEqual(app.current, "+33600000001")

            await pilot.press("ctrl+n")
            self.assertIsInstance(app.screen, NewConversation)
            await pilot.press(*"charl", "enter")
            await pilot.pause()
            self.assertEqual(app.current, "+33600000003")
            self.assertEqual(app.addresses[0], "+33600000003")  # draft on top
            self.assertEqual(app.focused.id, "compose")

            await pilot.press("ctrl+n", *"06 99 88 77 66", "enter")
            await pilot.pause()
            self.assertEqual(app.current, "+33699887766")
            await pilot.press("ctrl+n", "escape")
            self.assertNotIsInstance(app.screen, NewConversation)

            app.save_screenshot("ui.svg", path=self.shot_dir)

    async def test_send_offline_keeps_text(self):
        app = BtSms(fake_store(), connect=False)
        async with app.run_test() as pilot:
            await pilot.press(*"hello", "enter")
            self.assertEqual(app.query_one("#compose").value, "hello")

    shot_dir = __import__('os').environ.get('BTSMS_SHOT_DIR', '/tmp')




class TestIncoming(unittest.IsolatedAsyncioTestCase):
    async def test_toast_only_for_other_conversations(self):
        store = fake_store()
        app = BtSms(store, connect=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.current, "+33600000001")
            store.upsert_header("10", "inbox", "+33600000001", "Alice", time.time(), "là", False, False)
            store.upsert_header("11", "inbox", "+33600000002", "", time.time(), "Tu es où ?", False, False)
            store.commit()
            app.on_incoming(["10", "11"])
            app.schedule_refresh()
            await pilot.pause(0.2)
            toasts = list(app._notifications)
            self.assertEqual([t.title for t in toasts], ["Bob Durand"])
            self.assertEqual(toasts[0].message, "Tu es où ?")
            # The open conversation got the new message and was marked read.
            self.assertEqual(app.store.messages(app.current)[-1].body, "là")
            unread = {c.address: c.unread for c in app.store.conversations()}
            self.assertEqual(unread["+33600000001"], 0)
            self.assertEqual(unread["+33600000002"], 1)




class TestPaste(unittest.IsolatedAsyncioTestCase):
    async def test_ctrl_v_pastes_system_clipboard(self):
        import btsms.ui as ui
        original = ui.PASTE_COMMANDS
        ui.PASTE_COMMANDS = (["missing-tool"], ["printf", "collé 🚀\\nligne 2\\n"])
        self.addCleanup(setattr, ui, "PASTE_COMMANDS", original)
        app = BtSms(fake_store(), connect=False)
        async with app.run_test() as pilot:
            await pilot.press("a", "space", "ctrl+v")
            await pilot.pause()
            self.assertEqual(app.query_one("#compose").value, "a collé 🚀 ligne 2")


if __name__ == "__main__":
    unittest.main()
