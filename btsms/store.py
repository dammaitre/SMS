"""SQLite cache: messages, contacts, and the conversation view built from them."""

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .phone import display

DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "btsms"

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    handle   TEXT PRIMARY KEY,   -- MAP handle, or 'local-…' while a send is pending
    folder   TEXT NOT NULL,      -- inbox | sent | outbox
    address  TEXT NOT NULL,      -- normalised peer number (or alphanumeric sender)
    name     TEXT NOT NULL DEFAULT '',  -- name the phone gave in the listing
    ts       REAL NOT NULL,
    body     TEXT NOT NULL DEFAULT '',
    complete INTEGER NOT NULL DEFAULT 0, -- body fetched with Get (subject drops emoji)
    read     INTEGER NOT NULL DEFAULT 0,
    outgoing INTEGER NOT NULL DEFAULT 0,
    state    TEXT NOT NULL DEFAULT ''    -- '' | pending | failed (local sends)
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages (address, ts);
CREATE TABLE IF NOT EXISTS contacts (
    address TEXT PRIMARY KEY,
    name    TEXT NOT NULL
);
"""


@dataclass
class Conversation:
    address: str
    name: str
    last_ts: float
    last_body: str
    unread: int

    @property
    def title(self) -> str:
        return self.name or display(self.address)


@dataclass
class Msg:
    handle: str
    address: str
    ts: float
    body: str
    outgoing: bool
    read: bool
    state: str


class Store:
    def __init__(self, path: Path | str | None = None):
        if path is None:
            DATA.mkdir(parents=True, exist_ok=True)
            path = DATA / "btsms.db"
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def upsert_header(self, handle, folder, address, name, ts, subject, read, outgoing):
        """Insert a listed message; keep an already fetched body."""
        self.db.execute("""
            INSERT INTO messages (handle, folder, address, name, ts, body, read, outgoing)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (handle) DO UPDATE SET
                folder = excluded.folder, name = excluded.name, read = excluded.read,
                body = CASE WHEN complete THEN body ELSE excluded.body END
        """, (handle, folder, address, name, ts, subject, int(read), int(outgoing)))

    def has(self, handle: str) -> bool:
        return self.db.execute("SELECT 1 FROM messages WHERE handle = ?",
                               (handle,)).fetchone() is not None

    def set_body(self, handle: str, body: str):
        self.db.execute("UPDATE messages SET body = ?, complete = 1 WHERE handle = ?",
                        (body, handle))

    def incomplete(self, address: str | None = None, limit: int = 50) -> list[str]:
        """Handles whose body still needs a Get, newest first."""
        q = "SELECT handle FROM messages WHERE complete = 0 AND handle NOT LIKE 'local-%'"
        args: tuple = ()
        if address:
            q += " AND address = ?"
            args = (address,)
        q += " ORDER BY ts DESC LIMIT ?"
        return [r[0] for r in self.db.execute(q, args + (limit,))]

    def add_local(self, handle: str, address: str, body: str):
        self.db.execute("""
            INSERT INTO messages (handle, folder, address, ts, body, complete, read,
                                  outgoing, state)
            VALUES (?, 'outbox', ?, ?, ?, 1, 1, 1, 'pending')""",
                        (handle, address, time.time(), body))

    def set_state(self, handle: str, state: str):
        self.db.execute("UPDATE messages SET state = ? WHERE handle = ?", (state, handle))

    def drop_local_matching(self, address: str, body: str):
        """A pending local send showed up in 'sent' under its real handle."""
        self.db.execute("""DELETE FROM messages WHERE handle LIKE 'local-%'
                           AND address = ? AND body = ? AND state = 'pending'""",
                        (address, body))

    def mark_read(self, address: str) -> list[str]:
        """Mark a conversation read locally; returns handles that were unread."""
        rows = [r[0] for r in self.db.execute(
            "SELECT handle FROM messages WHERE address = ? AND read = 0", (address,))]
        self.db.execute("UPDATE messages SET read = 1 WHERE address = ?", (address,))
        return rows

    def set_contacts(self, pairs: list[tuple[str, str]]):
        self.db.execute("DELETE FROM contacts")
        self.db.executemany("INSERT OR REPLACE INTO contacts VALUES (?, ?)", pairs)

    def contacts(self) -> list[tuple[str, str]]:
        return self.db.execute("SELECT address, name FROM contacts ORDER BY name").fetchall()

    def conversations(self) -> list[Conversation]:
        rows = self.db.execute("""
            SELECT m.address,
                   COALESCE(c.name, (SELECT name FROM messages n WHERE n.address = m.address
                                     AND n.name != '' AND n.outgoing = 0 LIMIT 1), ''),
                   MAX(m.ts), m.body,
                   SUM(m.read = 0 AND m.outgoing = 0)
            FROM messages m LEFT JOIN contacts c ON c.address = m.address
            GROUP BY m.address ORDER BY MAX(m.ts) DESC
        """).fetchall()  # SQLite: bare m.body comes from the MAX(ts) row
        return [Conversation(*r) for r in rows]

    def messages(self, address: str) -> list[Msg]:
        rows = self.db.execute("""
            SELECT handle, address, ts, body, outgoing, read, state FROM messages
            WHERE address = ? ORDER BY ts""", (address,)).fetchall()
        return [Msg(h, a, ts, b, bool(o), bool(r), s) for h, a, ts, b, o, r, s in rows]

    def commit(self):
        self.db.commit()
