"""bMessage (MAP 1.x) build/parse, and the small vCard subset PBAP returns."""

from dataclasses import dataclass, field


@dataclass
class BMessage:
    folder: str = ""
    read: bool = False
    originator: dict = field(default_factory=dict)  # {"name", "tel"}
    recipient: dict = field(default_factory=dict)
    body: str = ""


def build(number: str, text: str) -> bytes:
    """Outgoing SMS for MessageAccess1.PushMessage (with Charset=utf8)."""
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    msg = f"BEGIN:MSG\r\n{text}\r\nEND:MSG\r\n"
    head = "\r\n".join([
        "BEGIN:BMSG", "VERSION:1.0", "STATUS:READ", "TYPE:SMS_GSM",
        "FOLDER:telecom/msg/outbox",
        "BEGIN:VCARD", "VERSION:2.1", "N:", "TEL:", "END:VCARD",
        "BEGIN:BENV",
        "BEGIN:VCARD", "VERSION:2.1", "N:", f"TEL:{number}", "END:VCARD",
        "BEGIN:BBODY", "CHARSET:UTF-8", f"LENGTH:{len(msg.encode())}",
    ])
    return (head + "\r\n" + msg + "END:BBODY\r\nEND:BENV\r\nEND:BMSG\r\n").encode()


def _vcard_props(lines: list[str]) -> dict:
    out = {}
    for line in lines:
        key, _, value = line.partition(":")
        key = key.split(";")[0].upper()
        if key == "FN" or (key == "N" and "name" not in out):
            out["name"] = value.replace(";", " ").strip()
        elif key == "TEL" and "tel" not in out:
            out["tel"] = value.strip()
    return out


def parse(data: str) -> BMessage:
    """Parse a bMessage as returned by Message1.Get (inbox or sent)."""
    lines = data.replace("\r\n", "\n").split("\n")
    m = BMessage()
    depth_env = 0
    card: list[str] | None = None
    body: list[str] = []
    chunks: list[str] = []
    in_msg = False
    for line in lines:
        if in_msg:
            if line == "END:MSG":
                chunks.append("\n".join(body))
                body, in_msg = [], False
            else:
                body.append(line)
            continue
        if line == "BEGIN:MSG":
            in_msg = True
        elif line == "BEGIN:BENV":
            depth_env += 1
        elif line == "END:BENV":
            depth_env -= 1
        elif line == "BEGIN:VCARD":
            card = []
        elif line == "END:VCARD" and card is not None:
            props = _vcard_props(card)
            # First vCard inside BENV is the recipient; outside, the originator.
            if depth_env and not m.recipient:
                m.recipient = props
            elif not depth_env and not m.originator:
                m.originator = props
            card = None
        elif card is not None:
            card.append(line)
        elif line.startswith("FOLDER:"):
            m.folder = line[7:]
        elif line.startswith("STATUS:"):
            m.read = line[7:] == "READ"
    m.body = "".join(chunks)  # long SMS may come as several MSG parts
    return m


def parse_vcards(data: str) -> list[dict]:
    """PBAP vCard 3.0 dump -> [{"name": str, "tels": [str]}]."""
    # Unfold continuation lines (RFC 2425 §5.8.1).
    text = data.replace("\r\n", "\n").replace("\n ", "").replace("\n\t", "")
    cards, cur = [], None
    for line in text.split("\n"):
        if line == "BEGIN:VCARD":
            cur = {"name": "", "tels": []}
        elif line == "END:VCARD" and cur is not None:
            cards.append(cur)
            cur = None
        elif cur is not None:
            key, _, value = line.partition(":")
            key = key.split(";")[0].upper()
            value = value.replace("\\,", ",").replace("\\;", ";").strip()
            if key == "FN" and value:
                cur["name"] = value
            elif key == "N" and not cur["name"]:
                cur["name"] = " ".join(p for p in reversed(value.split(";")) if p)
            elif key == "TEL" and value:
                cur["tels"].append(value)
    return cards
