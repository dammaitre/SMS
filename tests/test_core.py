import unittest

from btsms import bmessage
from btsms.map_client import parse_timestamp
from btsms.phone import display, normalize
from btsms.store import Store

INBOX = """BEGIN:BMSG\r
VERSION:1.0\r
STATUS:READ\r
TYPE:SMS_GSM\r
FOLDER:telecom/msg/inbox\r
BEGIN:VCARD\r
VERSION:3.0\r
FN:Péashouney\r
N:Péashouney\r
TEL:+33640000000\r
END:VCARD\r
BEGIN:BENV\r
BEGIN:BBODY\r
CHARSET:UTF-8\r
LENGTH:100\r
BEGIN:MSG\r
L'interface est stylée 🚀\r
deuxième ligne\r
END:MSG\r
END:BBODY\r
END:BENV\r
END:BMSG\r
"""

SENT = """BEGIN:BMSG
VERSION:1.0
STATUS:READ
TYPE:SMS_GSM
FOLDER:telecom/msg/sent
BEGIN:BENV
BEGIN:VCARD
VERSION:3.0
FN:Péashouney
N:Péashouney
TEL:+33640000000
END:VCARD
BEGIN:BBODY
CHARSET:UTF-8
LENGTH:37
BEGIN:MSG
Miam les tokens
END:MSG
END:BBODY
END:BENV
END:BMSG
"""


class TestPhone(unittest.TestCase):
    def test_normalize(self):
        for raw in ("06 12 34 56 78", "0612345678", "+33 6 12 34 56 78",
                    "0033612345678", "06.12.34.56.78"):
            self.assertEqual(normalize(raw), "+33612345678")
        self.assertEqual(normalize("AIRBNB"), "AIRBNB")
        self.assertEqual(normalize("BANQUE POP"), "BANQUE POP")
        self.assertEqual(normalize("36179"), "36179")

    def test_display(self):
        self.assertEqual(display("+33612345678"), "+33 6 12 34 56 78")
        self.assertEqual(display("AIRBNB"), "AIRBNB")


class TestBMessage(unittest.TestCase):
    def test_parse_inbox(self):
        m = bmessage.parse(INBOX)
        self.assertEqual(m.folder, "telecom/msg/inbox")
        self.assertEqual(m.originator, {"name": "Péashouney", "tel": "+33640000000"})
        self.assertEqual(m.body, "L'interface est stylée 🚀\ndeuxième ligne")

    def test_parse_sent(self):
        m = bmessage.parse(SENT)
        self.assertEqual(m.recipient["tel"], "+33640000000")
        self.assertEqual(m.body, "Miam les tokens")

    def test_build_roundtrip_and_length(self):
        text = "btsms test 🚀 éàç\nligne 2"
        raw = bmessage.build("+33612345678", text)
        s = raw.decode()
        msg = s[s.index("BEGIN:MSG"):s.index("END:BBODY")]
        length = int(s.split("LENGTH:")[1].split("\r\n")[0])
        self.assertEqual(length, len(msg.encode()))
        self.assertNotIn("\n", s.replace("\r\n", ""))  # CRLF only
        m = bmessage.parse(s)
        self.assertEqual(m.recipient["tel"], "+33612345678")
        self.assertEqual(m.body, text)

    def test_vcards(self):
        data = ("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Mon nom\r\nN:Mon nom\r\nEND:VCARD\r\n"
                "BEGIN:VCARD\r\nVERSION:3.0\r\nN:WU;Philippe;;;\r\nFN:Philippe WU\r\n"
                "TEL;TYPE=CELL;TYPE=PREF:+33782000000\r\nTEL;TYPE=HOME:01 23\r\n 45 67 89\r\n"
                "END:VCARD\r\n"
                "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Ruffin;Louise;;;\r\n"
                "TEL:0782000000\r\nEND:VCARD\r\n")
        cards = bmessage.parse_vcards(data)
        self.assertEqual(cards[0], {"name": "Mon nom", "tels": []})
        self.assertEqual(cards[1], {"name": "Philippe WU",
                                    "tels": ["+33782000000", "01 2345 67 89"]})
        self.assertEqual(cards[2]["name"], "Louise Ruffin")


class TestTimestamp(unittest.TestCase):
    def test_zones(self):
        self.assertEqual(parse_timestamp("20261019T085139Z"),
                         parse_timestamp("20261019T105139+0200"))
        self.assertGreater(parse_timestamp("20261019T085139"), 0)
        self.assertEqual(parse_timestamp(""), 0.0)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.s.upsert_header("1", "inbox", "+33600000001", "Alice", 100, "salut", False, False)
        self.s.upsert_header("2", "sent", "+33600000001", "Alice", 200, "ça va", True, True)
        self.s.upsert_header("3", "inbox", "AIRBNB", "AIRBNB", 150, "code", False, False)

    def test_conversations(self):
        convs = self.s.conversations()
        self.assertEqual([c.address for c in convs], ["+33600000001", "AIRBNB"])
        self.assertEqual(convs[0].last_body, "ça va")
        self.assertEqual(convs[0].unread, 1)
        self.assertEqual(convs[0].title, "Alice")

    def test_contact_name_wins_and_body_kept(self):
        self.s.set_contacts([("+33600000001", "Alice Martin")])
        self.s.set_body("1", "salut 🚀")
        self.s.upsert_header("1", "inbox", "+33600000001", "Alice", 100, "salut ", True, False)
        self.assertEqual(self.s.conversations()[0].title, "Alice Martin")
        self.assertEqual(self.s.messages("+33600000001")[0].body, "salut 🚀")
        self.assertEqual(self.s.incomplete(), ["2", "3"])

    def test_local_send_lifecycle(self):
        self.s.add_local("local-x", "+33600000001", "yo")
        self.assertEqual(self.s.messages("+33600000001")[-1].state, "pending")
        self.s.drop_local_matching("+33600000001", "yo")
        self.assertEqual(len(self.s.messages("+33600000001")), 2)

    def test_mark_read(self):
        self.assertEqual(self.s.mark_read("+33600000001"), ["1"])
        self.assertEqual(self.s.conversations()[0].unread, 0)


if __name__ == "__main__":
    unittest.main()
