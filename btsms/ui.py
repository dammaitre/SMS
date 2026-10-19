"""Textual front end: conversation list, read pane, input box, status bar."""

import asyncio
import logging
import shutil
import time

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .obex import find_device
from .phone import display, normalize
from .store import Msg, Store
from .sync import Sync

log = logging.getLogger(__name__)

NOTIFY_SEND = shutil.which("notify-send")
# Tried in order for Ctrl+V; Textual's own clipboard only holds text copied in-app.
PASTE_COMMANDS = (
    ["wl-paste", "--no-newline", "--type", "text"],
    ["xclip", "-selection", "clipboard", "-o"],
    ["xsel", "--clipboard", "--output"],
)


def fmt_time(ts: float) -> str:
    t = time.localtime(ts)
    return time.strftime("%H:%M" if t[:3] == time.localtime()[:3] else "%d/%m %H:%M", t)


def fmt_day(ts: float) -> str:
    return time.strftime("%A %d %B %Y", time.localtime(ts))


async def system_clipboard() -> str | None:
    for cmd in PASTE_COMMANDS:
        if not shutil.which(cmd[0]):
            continue
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 2)
        except (OSError, asyncio.TimeoutError):
            continue
        if proc.returncode == 0:
            return out.decode("utf-8", "replace")
    return None


class ComposeInput(Input):
    """Message box whose Ctrl+V pastes from the desktop clipboard."""

    async def action_paste(self):
        text = await system_clipboard()
        if text is None:
            text = self.app.clipboard
        start, end = self.selection
        self.replace(" ".join(text.split("\n")).strip("\r "), start, end)


class NewConversation(ModalScreen[str | None]):
    """Ctrl+N: pick a contact or type a number."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
        Binding("down", "move(1)", show=False, priority=True),
        Binding("up", "move(-1)", show=False, priority=True),
    ]

    def __init__(self, contacts: list[tuple[str, str]]):
        super().__init__()
        self.contacts = contacts

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("New conversation", id="dialog-title")
            yield Input(placeholder="Name or number", id="query")
            yield OptionList(id="matches")

    def on_mount(self):
        self.filter("")

    @on(Input.Changed, "#query")
    def filter(self, event_or_query):
        q = getattr(event_or_query, "value", event_or_query).strip().lower()
        digits = "".join(c for c in q if c.isdigit())
        hits = [(a, n) for a, n in self.contacts
                if not q or q in n.lower() or (digits and digits in a)][:50]
        matches = self.query_one("#matches", OptionList)
        matches.set_options(
            Option(Text.assemble((name, "bold"), "  ", (display(addr), "dim")), id=addr)
            for addr, name in hits)
        if hits:
            matches.highlighted = 0

    def action_move(self, delta: int):
        matches = self.query_one("#matches", OptionList)
        if matches.option_count:
            cur = matches.highlighted or 0
            matches.highlighted = max(0, min(matches.option_count - 1, cur + delta))

    @on(Input.Submitted, "#query")
    def submit(self, event: Input.Submitted):
        matches = self.query_one("#matches", OptionList)
        if matches.highlighted is not None and matches.option_count:
            self.dismiss(matches.get_option_at_index(matches.highlighted).id)
        elif any(c.isdigit() for c in event.value):
            self.dismiss(normalize(event.value))

    @on(OptionList.OptionSelected, "#matches")
    def picked(self, event: OptionList.OptionSelected):
        self.dismiss(event.option.id)


class BtSms(App):
    TITLE = "btsms"
    # One blue ($primary-darken-2) for everything "mine/active": selected
    # conversation, outgoing bubbles, focused input, unread badge.
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #convs { width: 34; border: none; border-right: solid $panel-lighten-2;
             background: $surface; padding: 0; }
    #convs > .option-list--option { padding: 0 1; }
    #convs > .option-list--option-highlighted,
    #convs:focus > .option-list--option-highlighted {
        background: $primary-darken-2; color: $text; text-style: none; }
    #right { width: 1fr; }
    #header { height: 1; padding: 0 2; background: $panel; text-style: bold; }
    #log { height: 1fr; padding: 0 1; }
    .day { width: 100%; content-align: center middle; color: $text-muted; margin: 1 0 0 0; }
    .row { height: auto; width: 100%; margin: 1 0 0 0; }
    .row.cont { margin: 0; }
    .row.out { align-horizontal: right; }
    .bubble { width: auto; max-width: 75%; padding: 0 1; background: $panel; }
    .out .bubble { background: $primary-darken-2; }
    .pending .bubble, .queued .bubble { color: $text-muted; }
    .failed .bubble { background: $error-darken-2; }
    #compose { dock: bottom; height: 3; margin: 1 1; padding: 0 1;
               border: tall $panel-lighten-2; background: $surface; }
    #compose:focus { border: tall $primary-darken-2; background: $surface; }
    #status { height: 1; padding: 0 1; background: $panel-darken-1; color: $text-muted; }
    NewConversation { align: center middle; }
    #dialog { width: 60; height: 24; border: thick $primary-darken-2; background: $surface;
              padding: 0 1; }
    #dialog-title { text-style: bold; padding: 0 0 1 0; }
    #query { border: tall $primary-darken-2; }
    #matches { height: 1fr; border: none; }
    #matches > .option-list--option-highlighted {
        background: $primary-darken-2; color: $text; text-style: none; }
    """
    BINDINGS = [
        Binding("ctrl+up", "move(-1)", "Prev", priority=True),
        Binding("ctrl+down", "move(1)", "Next", priority=True),
        Binding("alt+up", "move(-1)", show=False, priority=True),
        Binding("alt+down", "move(1)", show=False, priority=True),
        Binding("ctrl+n", "new", "New", priority=True),
        Binding("ctrl+r", "retry", "Retry", priority=True),
        Binding("pageup", "scroll(-1)", show=False, priority=True),
        Binding("pagedown", "scroll(1)", show=False, priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, store: Store | None = None, connect: bool = True):
        super().__init__()
        self.store = store or Store()
        self.connect = connect
        self.sync: Sync | None = None
        self.addresses: list[str] = []  # order shown in #convs
        self.current: str | None = None
        self.draft: str | None = None  # Ctrl+N target with no messages yet
        self._shown: tuple = ()
        self._refresh_pending = False
        self._status = "offline (cache)"
        self._alias = ""
        self._focused = True  # terminal focus, if the terminal reports it

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            yield OptionList(id="convs")
            with Vertical(id="right"):
                yield Static("", id="header")
                yield VerticalScroll(id="log")
                yield ComposeInput(placeholder="Message…  (Enter to send)", id="compose")
        yield Static("", id="status")

    def on_mount(self):
        self.query_one("#convs").can_focus = False
        self.query_one("#log").can_focus = False
        self.query_one("#compose").focus()
        self.refresh_views()
        if self.connect:
            self.run_worker(self.start_sync(), exclusive=True, group="sync")

    # ------------------------------------------------------------ sync

    def set_status(self, text: str):
        self._status = text
        self.query_one("#status", Static).update(
            text + "   ctrl+↑↓ switch · ctrl+n new · enter send · ctrl+r retry · ctrl+q quit")

    async def start_sync(self):
        while True:
            try:
                address, self._alias = await find_device()
                break
            except Exception as e:
                self.set_status(f"✕ {e} · retrying")
                await asyncio.sleep(10)
        self.sync = Sync(self.store, address)
        self.sync.listeners.append(self.schedule_refresh)
        self.sync.incoming.append(self.on_incoming)
        self.sync.on_state.append(self.on_sync_state)
        await self.sync.run()

    def on_sync_state(self, state: str):
        if state == "connected":
            text = f"● {self._alias}"
        elif state == "connecting":
            text = f"… connecting to {self._alias}"
        elif state.startswith("offline"):
            text = f"○ {self._alias} {state}"
        else:
            text = f"● {self._alias} · {state}"
        self.set_status(text)

    def on_app_focus(self):
        self._focused = True

    def on_app_blur(self):
        self._focused = False

    def on_incoming(self, handles: list[str]):
        """Ring; toast unless it is the open conversation; desktop pop-up if unfocused."""
        self.bell()
        for handle in handles:
            m = self.store.get(handle)
            if not m:
                continue
            title, body = self.title_for(m.address), " ".join(m.body.split())[:120] or "(empty)"
            if m.address != self.current:
                self.notify(body, title=title, timeout=8)
            if not self._focused and NOTIFY_SEND:
                self.run_worker(self.desktop_notify(title, body), group="notify")

    async def desktop_notify(self, title: str, body: str):
        proc = await asyncio.create_subprocess_exec(
            NOTIFY_SEND, "-a", "btsms", "-i", "mail-message-new", title, body)
        await proc.wait()

    def schedule_refresh(self):
        if not self._refresh_pending:
            self._refresh_pending = True
            self.set_timer(0.05, self.refresh_views)

    # ------------------------------------------------------------ views

    def refresh_views(self):
        self._refresh_pending = False
        convs = self.store.conversations()
        badge = f"bold {self.get_css_variables()['primary-lighten-1']}"
        self.addresses = [c.address for c in convs]
        options = []
        if self.draft and self.draft not in self.addresses:
            self.addresses.insert(0, self.draft)
            options.append(Option(Text.assemble((self.title_for(self.draft), "bold"),
                                                "\n", ("new conversation", "dim italic"))))
        else:
            self.draft = None
        for c in convs:
            preview = " ".join(c.last_body.split())
            line1 = Text(c.title[:24], style="bold" if c.unread else "")
            line1.append(" " * max(1, 31 - len(line1) - len(fmt_time(c.last_ts))))
            line1.append(fmt_time(c.last_ts), style="dim")
            line2 = Text(preview[:30] or " ", style="dim")
            if c.unread:
                line2 = Text.assemble((f"● {c.unread} ", badge), preview[:26])
            options.append(Option(Text.assemble(line1, "\n", line2)))
        convlist = self.query_one("#convs", OptionList)
        convlist.set_options(options)
        if self.current not in self.addresses:
            self.current = self.addresses[0] if self.addresses else None
        if self.current:
            convlist.highlighted = self.addresses.index(self.current)
        self.show_messages()
        self.set_status(self._status)

    def title_for(self, address: str) -> str:
        names = dict(self.store.contacts())
        conv = next((c for c in self.store.conversations() if c.address == address), None)
        return names.get(address) or (conv.title if conv else display(address))

    def show_messages(self):
        header = self.query_one("#header", Static)
        log_ = self.query_one("#log", VerticalScroll)
        if not self.current:
            header.update("No conversation — ctrl+n to start one")
            log_.remove_children()
            self._shown = ()
            return
        title = self.title_for(self.current)
        number = display(self.current)
        header.update(title if title == number else f"{title}  [dim]{number}[/]")
        msgs = self.store.messages(self.current)
        key = (self.current, tuple((m.handle, m.body, m.state) for m in msgs))
        if key == self._shown:
            return
        self._shown = key
        log_.remove_children()
        log_.mount_all(self.message_widgets(msgs))
        log_.scroll_end(animate=False)
        if any(not m.read for m in msgs):
            if self.sync:
                self.run_worker(self.sync.mark_read(self.current), group="read")
            else:
                self.store.mark_read(self.current)
                self.store.commit()
                self.schedule_refresh()

    def message_widgets(self, msgs: list[Msg]):
        last_day = last_out = None
        for m in msgs:
            day = time.localtime(m.ts)[:3]
            same_side = m.outgoing == last_out
            if day != last_day:
                last_day, same_side = day, False
                yield Static(fmt_day(m.ts), classes="day")
            last_out = m.outgoing
            stamp = fmt_time(m.ts)
            if m.state == "queued":
                stamp = "queued · sent when the phone is back"
            elif m.state == "pending":
                stamp = "sending…"
            elif m.state == "failed":
                stamp = "✕ not sent · ctrl+r to retry"
            body = Text(m.body or "(empty)")
            body.append(f"\n{stamp}", style="dim")
            classes = ("row " + ("out" if m.outgoing else "in") + (" cont" if same_side else "")
                       + (f" {m.state}" if m.state else ""))
            yield Horizontal(Static(body, classes="bubble"), classes=classes)

    def select(self, address: str):
        if address == self.current:
            return
        self.current = address
        self.show_messages()
        if self.sync and self.sync.connected and self.store.incomplete(address, limit=1):
            self.run_worker(self.sync.fetch_bodies(address=address), group="bodies")

    # ------------------------------------------------------------ actions

    def action_move(self, delta: int):
        if not self.addresses:
            return
        i = self.addresses.index(self.current) if self.current in self.addresses else 0
        i = max(0, min(len(self.addresses) - 1, i + delta))
        self.query_one("#convs", OptionList).highlighted = i
        self.select(self.addresses[i])

    def action_retry(self):
        if not (self.sync and self.current and self.store.locals("failed", self.current)):
            return

        async def do_retry():
            try:
                await self.sync.retry(self.current)
            except Exception as e:
                self.notify(f"Send failed: {e}", severity="error")
        self.run_worker(do_retry(), group="send")

    def action_scroll(self, direction: int):
        log_ = self.query_one("#log", VerticalScroll)
        (log_.scroll_page_down if direction > 0 else log_.scroll_page_up)(animate=False)

    def action_new(self):
        def done(address: str | None):
            if address:
                if address not in self.addresses:
                    self.draft = address
                self.current = address
                self.refresh_views()
            self.query_one("#compose").focus()
        self.push_screen(NewConversation(self.store.contacts()), done)

    @on(OptionList.OptionSelected, "#convs")
    @on(OptionList.OptionHighlighted, "#convs")
    def clicked(self, event):
        if 0 <= event.option_index < len(self.addresses):
            self.select(self.addresses[event.option_index])
        self.query_one("#compose").focus()

    @on(Input.Submitted, "#compose")
    async def send(self, event: Input.Submitted):
        text = event.value.strip()
        if not text or not self.current:
            return
        if not self.sync:
            self.notify("Not connected to the phone", severity="error")
            return
        event.input.value = ""
        address = self.current
        self.draft = None

        async def do_send():
            try:
                await self.sync.send(address, text)
            except Exception as e:
                self.notify(f"Send failed: {e}", severity="error")
        self.run_worker(do_send(), group="send")
