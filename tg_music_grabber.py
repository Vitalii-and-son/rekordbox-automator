#!/usr/bin/env python3
"""
Telegram Spotify-song grabber  ->  import into a desktop program.

Pipeline (one link at a time):
  1. Send a Spotify link to a Telegram music bot, using YOUR user account (Telethon).
  2. Click the bot's inline button(s) (quality / download), if it shows any.
  3. Download the audio file(s) the bot sends back into DOWNLOAD_DIR
     (several in parallel, bounded by DOWNLOAD_CONCURRENCY).
  4. GUI-automate importing each downloaded file into another PC program
     (default: focus its window -> Ctrl+O open dialog -> type path -> Enter).

Run:
    python tg_music_grabber.py "https://open.spotify.com/track/xxxx"
    python tg_music_grabber.py                 # reads links.txt, or prompts you

First run asks for your phone number + Telegram login code (once). A local
".session" file is saved so later runs log in automatically.

Everything you must fill in is in the CONFIG block below (or via env vars).
"""

import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError


def _load_dotenv():
    """Load KEY=VALUE lines from a local .env next to this script (if present).

    Keeps secrets (api_hash etc.) out of this tracked file. Real environment
    variables take precedence over .env values.
    """
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

# =====================================================================
# CONFIG  -- edit these, or set the matching TG_* environment variables
# =====================================================================

# --- Telegram API credentials -------------------------------------------------
# Get these once from https://my.telegram.org  ->  "API development tools".
API_ID = int(os.environ.get("TG_API_ID", "0"))          # <-- FILL IN (a number)
API_HASH = os.environ.get("TG_API_HASH", "")            # <-- FILL IN (a hex string)
SESSION_NAME = os.environ.get("TG_SESSION", "tg_music_grabber")

# --- The music bot you paste Spotify links to ---------------------------------
BOT_USERNAME = os.environ.get("TG_BOT", "@YourSpotifyDownloaderBot")  # <-- FILL IN

# --- Where downloaded songs are saved -----------------------------------------
DOWNLOAD_DIR = Path(os.environ.get(
    "TG_DOWNLOAD_DIR",
    str(Path.home() / "Music" / "TelegramSongs"),
))

# --- Which inline button to click ---------------------------------------------
# The bot may reply with buttons (e.g. "MP3 320", "Download", "High quality").
# BUTTON_MATCH is a regex tried against each button's text (case-insensitive).
# Leave empty to just click the first button when AUTO_CLICK_FIRST_BUTTON is True.
BUTTON_MATCH = os.environ.get("TG_BUTTON_MATCH", "")     # e.g. "320|high|mp3|download"
AUTO_CLICK_FIRST_BUTTON = True

# --- Timing -------------------------------------------------------------------
# We consider the bot "done" only after it has been fully silent (no message AND
# no "typing/uploading" indicator) for IDLE_TIMEOUT, so slow bots that pause to
# prepare the next track aren't cut off mid-batch. STALL_TIMEOUT is a safety net:
# give up only if NOTHING happens (no bot activity, no download progress) for that
# long -- so a big playlist that legitimately takes a while is never abandoned.
IDLE_TIMEOUT = int(os.environ.get("TG_IDLE_TIMEOUT", "90"))        # silence => bot is done
STALL_TIMEOUT = int(os.environ.get("TG_STALL_TIMEOUT",
                    os.environ.get("TG_OVERALL_TIMEOUT", "600")))  # no activity at all => bail
HISTORY_LIMIT = int(os.environ.get("TG_HISTORY_LIMIT", "200"))     # --history: msgs to scan

# --- Concurrency --------------------------------------------------------------
# Songs download in parallel, but BOUNDED: too many at once on one connection
# just trips Telegram's flood limits and gets slower (or temporarily blocked).
DOWNLOAD_CONCURRENCY = max(1, int(os.environ.get("TG_DOWNLOAD_CONCURRENCY", "4")))
SEND_STAGGER = float(os.environ.get("TG_SEND_STAGGER", "1.0"))    # sec between link sends

# --- Destination program (GUI automation) -------------------------------------
# The window title is matched as a substring, e.g. "rekordbox", "Serato",
# "iTunes", "Plex". PROGRAM_EXE is optional: launched only if the window
# isn't already open. Leave PROGRAM_WINDOW_TITLE empty to skip auto-import
# (files are just downloaded and listed).
PROGRAM_WINDOW_TITLE = os.environ.get("TG_PROGRAM_TITLE", "")     # <-- FILL IN
PROGRAM_EXE = os.environ.get("TG_PROGRAM_EXE", "")               # optional
IMPORT_HOTKEY = tuple(k.strip().lower() for k in
                      os.environ.get("TG_IMPORT_HOTKEY", "ctrl,o").split(",") if k.strip())

# "folder" = bulk: one Open dialog, click the file list, Ctrl+A (select all), Enter --
# imports the whole folder in one shot. "file" = the old one-dialog-per-track way.
IMPORT_MODE = os.environ.get("TG_IMPORT_MODE", "folder").lower()
IMPORT_LIMIT = int(os.environ.get("TG_IMPORT_LIMIT", "0"))  # >0 = only import this many (dry run)
# Where to click to focus the Open dialog's file list, as fractions of the APP
# WINDOW (the dialog centers on it). (0.5, 0.5) = window centre, which is where a
# centered Open dialog's file list sits. Tune only if the click misses the list.
try:
    IMPORT_LIST_CLICK = tuple(float(x) for x in
                              os.environ.get("TG_IMPORT_LIST_CLICK", "0.5,0.5").split(","))[:2]
except ValueError:
    IMPORT_LIST_CLICK = (0.5, 0.5)
# Seconds to pause with everything selected BEFORE pressing Enter, so you can eyeball
# the dialog on your first run (mouse to a screen corner aborts). 0 = import instantly.
IMPORT_PAUSE = float(os.environ.get("TG_IMPORT_PAUSE", "0"))

AUDIO_EXTS = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus", ".aac")
ARCHIVE_EXTS = (".zip", ".rar", ".7z")   # playlist downloads often arrive zipped

# =====================================================================
# Telegram side
# =====================================================================

def is_wanted_media(msg) -> bool:
    """True if this bot message carries a song (or an archive of songs) to save."""
    if getattr(msg, "audio", None) or getattr(msg, "voice", None):
        return True
    doc = getattr(msg, "document", None)
    if doc:
        if doc.mime_type and doc.mime_type.startswith("audio"):
            return True
        for attr in doc.attributes:
            name = getattr(attr, "file_name", "") or ""
            if name.lower().endswith(AUDIO_EXTS + ARCHIVE_EXTS):
                return True
    return False


def wanted_target(msg):
    """Return (destination Path, expected size) for a media message, else (None, None).

    Lets us skip files already fully downloaded, so re-runs resume instead of
    re-fetching everything. A size mismatch means a partial file -> re-download.
    """
    doc = getattr(msg, "document", None)
    if doc:
        size = getattr(doc, "size", None)
        for attr in doc.attributes:
            name = getattr(attr, "file_name", None)
            if name:
                return DOWNLOAD_DIR / name, size
    return None, None


def new_results():
    """Fresh accumulator for a run's download outcomes."""
    return {"files": [], "downloaded": [], "skipped": [], "failed": []}


async def download_one(client, msg, results, progress_cb=None):
    """Download one song/archive message; record the outcome in `results`.

    Skips a file we already have whole, re-fetches partial files, and never lets
    one failed download abort the whole batch. progress_cb (if given) is called
    with (received, total) as bytes arrive -- used to prove a long download is
    still alive so the run doesn't declare a stall.
    """
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target, expected = wanted_target(msg)
    name = target.name if target else f"message {msg.id}"

    if target and expected and target.exists() and target.stat().st_size == expected:
        print(f"    skip (already have): {name}")
        results["skipped"].append(name)
        results["files"].append(target)
        return

    dest = str(target) if target else str(DOWNLOAD_DIR) + os.sep
    try:
        path = await client.download_media(msg, file=dest, progress_callback=progress_cb)
    except FloodWaitError as e:
        wait = e.seconds + 1
        print(f"    flood-wait {wait}s on {name}; backing off then retrying ...")
        await asyncio.sleep(wait)
        try:
            path = await client.download_media(msg, file=dest, progress_callback=progress_cb)
        except Exception as e2:
            print(f"    [!] FAILED: {name}  ({e2})")
            results["failed"].append((name, str(e2)))
            return
    except Exception as e:
        print(f"    [!] FAILED: {name}  ({e})")
        results["failed"].append((name, str(e)))
        return

    if path:
        print(f"    downloaded: {Path(path).name}")
        results["downloaded"].append(Path(path).name)
        results["files"].append(Path(path))
    else:
        print(f"    [!] FAILED (nothing returned): {name}")
        results["failed"].append((name, "no file returned"))


async def maybe_click_button(msg, pattern: str, already_clicked: set):
    """Click the best not-yet-clicked callback button. Returns its text, or None.

    URL buttons are ignored (we only want to trigger the bot's callbacks).
    Tracking by (msg.id, text) handles multi-step flows where the bot edits one
    message from format buttons -> a 'Start Download' button.
    """
    if not msg.buttons:
        return None
    candidates = [
        b for row in msg.buttons for b in row
        if getattr(b, "text", None)
        and not getattr(b, "url", None)
        and (msg.id, b.text) not in already_clicked
    ]
    if not candidates:
        return None
    target = None
    if pattern:
        rx = re.compile(pattern, re.I)
        target = next((b for b in candidates if rx.search(b.text)), None)
    if target is None and AUTO_CLICK_FIRST_BUTTON:
        target = candidates[0]
    if target is None:
        return None
    print(f"    clicking button: {target.text!r}")
    try:
        await target.click()
        return target.text
    except Exception as e:
        print(f"    [!] button click failed: {e}")
        return None


class DownloadPool:
    """Runs song downloads with bounded concurrency, de-duplicated by filename.

    submit() fires a download off in the background (up to DOWNLOAD_CONCURRENCY
    run at once; the rest wait on the semaphore); join() waits for whatever is
    still running. The same file arriving twice (e.g. the bot re-sends it) is
    only downloaded once.
    """

    def __init__(self, client, results, concurrency, progress_cb=None):
        self.client = client
        self.results = results
        self.progress_cb = progress_cb
        self.sem = asyncio.Semaphore(concurrency)
        self.scheduled = set()   # target filenames already downloaded / in flight
        self.tasks = set()       # in-flight download tasks

    def submit(self, msg):
        target, _ = wanted_target(msg)
        key = target.name if target else f"msg-{msg.id}"
        if key in self.scheduled:
            return
        self.scheduled.add(key)
        task = asyncio.create_task(self._run(msg, key))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run(self, msg, key):
        async with self.sem:
            failed_before = len(self.results["failed"])
            try:
                await download_one(self.client, msg, self.results, self.progress_cb)
            except Exception as e:
                # download_one guards the transfer itself, but the mkdir / skip
                # check ahead of it can still raise. Record it -- never let it
                # vanish silently into an unretrieved task exception.
                name = f"message {getattr(msg, 'id', '?')}"
                print(f"    [!] FAILED: {name}  ({e})")
                self.results["failed"].append((name, str(e)))
            if len(self.results["failed"]) > failed_before:
                # This attempt failed; forget the filename so a later re-send of
                # the same track gets another try this run instead of being dropped.
                self.scheduled.discard(key)

    @property
    def pending(self):
        return len(self.tasks)

    async def join(self, cancel=False):
        if cancel:
            for t in self.tasks:
                t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


async def grab_all(client: TelegramClient, bot, urls, results):
    """Send every link, click buttons, and download the returned songs in parallel.

    One event handler feeds a single message stream (sending each link its own
    coroutine would make every handler see every message and download it N times).
    Media messages go to a bounded DownloadPool, so several songs download at once
    while later links are still being sent.

    We keep going until the bot is genuinely finished. "Finished" means it has been
    fully silent for IDLE_TIMEOUT -- no new/edited messages AND no "typing/uploading"
    indicator -- with every link sent and no downloads in flight. A bot that pauses
    to prepare or upload the next track keeps the run alive (its upload indicator and
    our own download progress both count as activity), so slow batches aren't cut off.
    STALL_TIMEOUT only trips if truly nothing happens for a long time.
    """
    queue: asyncio.Queue = asyncio.Queue()
    seen = set()             # (msg id, edit_date) states already processed
    clicked_buttons = set()  # (msg id, button text) we've already clicked
    loop = asyncio.get_event_loop()
    last_activity = loop.time()   # last time the bot did ANYTHING, or a download progressed

    def stamp(*_):
        nonlocal last_activity
        last_activity = loop.time()

    pool = DownloadPool(client, results, DOWNLOAD_CONCURRENCY, progress_cb=stamp)
    bot_id = getattr(bot, "id", None)

    async def handler(event):
        stamp()
        await queue.put(event.message)

    async def activity_handler(event):
        # Bot started typing / uploading a file (the "sending..." you see in the
        # Telegram UI). It's still working -> don't count this as silence.
        if getattr(event, "user_id", None) == bot_id and getattr(event, "action", None):
            stamp()

    client.add_event_handler(handler, events.NewMessage(from_users=bot))
    client.add_event_handler(handler, events.MessageEdited(from_users=bot))
    client.add_event_handler(activity_handler, events.UserUpdate)

    async def send_links():
        for i, url in enumerate(urls):
            if i:
                await asyncio.sleep(SEND_STAGGER)  # spread sends out; be nice to the bot
            print(f"  -> sent: {url}")
            try:
                await client.send_message(bot, url)
            except FloodWaitError as e:
                print(f"  [!] flood control: waiting {e.seconds}s before retrying that send ...")
                await asyncio.sleep(e.seconds + 1)
                await client.send_message(bot, url)

    sender = asyncio.create_task(send_links())
    poll = min(IDLE_TIMEOUT, 5)   # re-check the idle/stall conditions this often
    stalled = False

    try:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=poll)
            except asyncio.TimeoutError:
                idle = loop.time() - last_activity
                # Done: every link sent, nothing downloading, bot fully quiet a while.
                if sender.done() and pool.pending == 0 and idle >= IDLE_TIMEOUT:
                    break
                # Safety net: nothing at all has happened for a very long time.
                if idle >= STALL_TIMEOUT:
                    print(f"  [!] no activity for {STALL_TIMEOUT}s; giving up on the rest.")
                    stalled = True
                    break
                continue

            key = (msg.id, getattr(msg, "edit_date", None))
            if key in seen:
                continue
            seen.add(key)

            if is_wanted_media(msg):
                pool.submit(msg)
                continue

            clicked_text = None
            if msg.buttons:
                clicked_text = await maybe_click_button(msg, BUTTON_MATCH, clicked_buttons)
                if clicked_text is not None:
                    clicked_buttons.add((msg.id, clicked_text))

            if clicked_text is None and msg.message:
                print(f"    bot: {msg.message[:100]}")
    finally:
        client.remove_event_handler(handler)
        client.remove_event_handler(activity_handler)
        if not sender.done():
            sender.cancel()
        try:
            await sender  # surface send errors / absorb the cancellation
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"  [!] error while sending links: {e}")
        if pool.pending:
            action = "cancelling" if stalled else "finishing"
            print(f"  {action} {pool.pending} download(s) still in progress ...")
        await pool.join(cancel=stalled)


async def grab_history(client, bot, limit, results):
    """Download files the bot has ALREADY sent into the chat (no re-triggering)."""
    print(f"Scanning the last {limit} messages from the bot for songs ...")
    pool = DownloadPool(client, results, DOWNLOAD_CONCURRENCY)
    async for msg in client.iter_messages(bot, limit=limit):
        if is_wanted_media(msg):
            pool.submit(msg)
    if pool.pending:
        print(f"  downloading {pool.pending} song(s), up to {DOWNLOAD_CONCURRENCY} at a time ...")
    await pool.join()


# =====================================================================
# Desktop-program side (GUI automation)
# =====================================================================

def _pick_app_window(wins):
    """Choose the real destination-app window from a title-substring match.

    "rekordbox" also matches this project's VS Code / File Explorer windows
    ("rekordbox-automator"), so prefer an exact title, then drop obvious editor /
    explorer / browser windows, then take the shortest title (the bare app).
    """
    tl = PROGRAM_WINDOW_TITLE.strip().lower()
    exact = [w for w in wins if (w.title or "").strip().lower() == tl]
    if exact:
        return exact[0]
    junk = ("visual studio code", "file explorer", "- notepad", "google chrome",
            "microsoft edge", "firefox", "- powershell", "command prompt", "automator")
    filtered = [w for w in wins if not any(j in (w.title or "").lower() for j in junk)]
    pool = filtered or wins
    return min(pool, key=lambda w: len(w.title or ""))


def focus_program():
    """Bring the destination program to the foreground (launching it if needed).

    Returns the chosen window (truthy) so callers can click relative to it, or
    False if none could be found/opened.
    """
    if not PROGRAM_WINDOW_TITLE:
        return False
    try:
        import pygetwindow as gw
    except ImportError:
        print("    [!] pygetwindow not installed; cannot focus window.")
        return False

    wins = gw.getWindowsWithTitle(PROGRAM_WINDOW_TITLE)
    if not wins and PROGRAM_EXE:
        print(f"    launching {PROGRAM_EXE} ...")
        try:
            subprocess.Popen([PROGRAM_EXE])
            time.sleep(8)
            wins = gw.getWindowsWithTitle(PROGRAM_WINDOW_TITLE)
        except Exception as e:
            print(f"    [!] could not launch program: {e}")

    if not wins:
        print(f"    [!] no window titled ~'{PROGRAM_WINDOW_TITLE}' found.")
        return False

    w = _pick_app_window(wins)
    try:
        if getattr(w, "isMinimized", False):
            w.restore()
        w.activate()
    except Exception:
        # activate() is flaky on Windows; a click usually still lands on the app
        pass
    time.sleep(0.6)
    return w


def _wait_for_dialog(gw, before_hwnds, timeout=5.0):
    """After a Ctrl+O, return the newly-opened dialog window (or None).

    Diffs the set of top-level windows against a snapshot taken just before the
    hotkey, so we can (a) confirm the Open dialog actually opened and (b) click its
    real centre instead of guessing where the file list is.
    """
    steps = max(1, int(timeout / 0.3))
    for _ in range(steps):
        time.sleep(0.3)
        try:
            current = gw.getAllWindows()
        except Exception:
            return None
        new = [w for w in current
               if getattr(w, "_hWnd", None) not in before_hwnds
               and (w.title or "").strip()
               and getattr(w, "width", 0) > 200 and getattr(w, "height", 0) > 150]
        if new:
            return max(new, key=lambda w: w.width * w.height)  # dialog = the big new window
    return None


# Set-1 keyboard scan codes for the keys we may need in a shortcut.
_SCANCODES = {
    "ctrl": 0x1D, "control": 0x1D, "shift": 0x2A, "alt": 0x38,
    "a": 0x1E, "b": 0x30, "c": 0x2E, "d": 0x20, "e": 0x12, "f": 0x21, "g": 0x22,
    "h": 0x23, "i": 0x17, "j": 0x24, "k": 0x25, "l": 0x26, "m": 0x32, "n": 0x31,
    "o": 0x18, "p": 0x19, "q": 0x10, "r": 0x13, "s": 0x1F, "t": 0x14, "u": 0x16,
    "v": 0x2F, "w": 0x11, "x": 0x2D, "y": 0x15, "z": 0x2C,
}


def _scancode_hotkey(keys):
    """Send a chord (e.g. ('ctrl','o')) as hardware SCAN codes via Win32 SendInput.

    Apps that read the keyboard at a low level (rekordbox, games) ignore the
    virtual-key events pyautogui injects but DO accept scan codes. Returns True if
    injected, False if unavailable (non-Windows / unknown key / API refused) so the
    caller can fall back to pyautogui.
    """
    if not sys.platform.startswith("win"):
        return False
    keys = [k.strip().lower() for k in keys if k.strip()]
    codes = [_SCANCODES.get(k) for k in keys]
    if not codes or any(c is None for c in codes):
        missing = [k for k, c in zip(keys, codes) if c is None]
        if missing:
            print(f"    [!] no scancode for {missing}; falling back to pyautogui keys.")
        return False
    try:
        import ctypes
        from ctypes import wintypes

        KEYEVENTF_SCANCODE = 0x0008
        KEYEVENTF_KEYUP = 0x0002
        INPUT_KEYBOARD = 1
        ULONG_PTR = wintypes.WPARAM  # pointer-sized integer (correct on 32/64-bit)

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ULONG_PTR)]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class _INPUTunion(ctypes.Union):
            # MOUSEINPUT is the largest member; include it so sizeof(INPUT) matches
            # what SendInput's cbSize expects (40 on x64), or the call silently fails.
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        SendInput = user32.SendInput
        SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        SendInput.restype = wintypes.UINT

        def _send(scan, up):
            u = _INPUTunion()
            u.ki = KEYBDINPUT(0, scan, KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
            inp = INPUT(INPUT_KEYBOARD, u)
            n = SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            if n == 0:
                print(f"    [!] SendInput rejected a key (WinError {ctypes.get_last_error()}).")
            return n

        ok = 1
        for c in codes[:-1]:                 # modifiers down
            ok &= _send(c, False); time.sleep(0.02)
        ok &= _send(codes[-1], False); time.sleep(0.03)   # main key down
        ok &= _send(codes[-1], True); time.sleep(0.02)     # main key up
        for c in reversed(codes[:-1]):       # modifiers up
            ok &= _send(c, True); time.sleep(0.02)
        return bool(ok)
    except Exception as e:
        print(f"    [!] scancode send failed ({e}); falling back to pyautogui keys.")
        return False


def _send_hotkey(pyautogui, keys):
    """Open the dialog via a hotkey.

    Prefer hardware SCAN codes (rekordbox ignores pyautogui's virtual-key events);
    fall back to pyautogui key-holds if scancode injection isn't available.
    """
    if _scancode_hotkey(keys):
        return
    mods, main = keys[:-1], keys[-1]
    for m in mods:
        pyautogui.keyDown(m)
        time.sleep(0.05)
    pyautogui.keyDown(main); time.sleep(0.08); pyautogui.keyUp(main); time.sleep(0.05)
    for m in reversed(mods):
        pyautogui.keyUp(m)


def import_into_program(file_path: Path):
    """
    Feed one downloaded song into the destination program.

    Default strategy = the most portable one: focus the app, open its
    File-Open dialog with a hotkey (Ctrl+O), type the full path, press Enter.
    If YOUR program imports differently (a menu, drag-and-drop, a specific
    button), tell me its name and I'll replace this function with the exact steps.
    """
    try:
        import pyautogui
    except ImportError:
        print("    [!] pyautogui not installed; cannot GUI-import.")
        return
    try:
        import pygetwindow as gw
    except ImportError:
        gw = None

    pyautogui.FAILSAFE = True  # slam mouse to a screen corner to abort
    win = focus_program()
    if not win:
        print(f"    [!] skipping import of {file_path.name} (program not focused).")
        return

    # Click the app first so it truly holds keyboard focus, else Ctrl+O is dropped.
    try:
        pyautogui.click(int(win.left + win.width * 0.5), int(win.top + win.height * 0.5))
        time.sleep(0.3)
    except Exception:
        pass
    time.sleep(0.7)
    before = {getattr(w, "_hWnd", None) for w in gw.getAllWindows()} if gw else set()
    _send_hotkey(pyautogui, IMPORT_HOTKEY)   # open the file dialog
    dialog = _wait_for_dialog(gw, before, timeout=4.0) if gw else None
    if gw is not None and dialog is None:
        print(f"    [!] no Open dialog appeared; skipping {file_path.name} (won't blind-type).")
        return
    if dialog is None:
        time.sleep(1.5)
    pyautogui.write(str(file_path), interval=0.02)
    time.sleep(0.4)
    pyautogui.press("enter")
    time.sleep(1.5)
    print(f"    imported into program: {file_path.name}")


def import_files_bulk(files):
    """Import a whole folder of songs in ONE Open dialog per folder.

    Mirrors the manual flow confirmed to work in rekordbox: open the file dialog
    (Ctrl+O), navigate into the folder, click the file list, Ctrl+A to select
    everything shown, Enter to import. rekordbox ignores any non-audio file in the
    folder, so 125 tracks go in with a single dialog. Where to click the list is
    TG_IMPORT_LIST_CLICK; TG_IMPORT_PAUSE holds before Enter so you can verify.
    """
    try:
        import pyautogui
    except ImportError:
        print("    [!] pyautogui not installed; cannot GUI-import.")
        return

    audio = [f for f in files if f.suffix.lower() in AUDIO_EXTS]
    if not audio:
        print("    nothing to import (no audio files).")
        return
    folders = sorted({f.parent for f in audio}, key=str)

    pyautogui.FAILSAFE = True  # slam mouse to a screen corner to abort
    try:
        import pygetwindow as gw
    except ImportError:
        gw = None

    win = focus_program()
    if not win:
        print("    [!] could not focus the program; import aborted.")
        return

    # Fallback list-click point (app-window centre), used only if we can't detect
    # the dialog window itself. Fractions are of the app window.
    fx, fy = IMPORT_LIST_CLICK
    appx = int(win.left + win.width * fx)
    appy = int(win.top + win.height * fy)

    for folder in folders:
        n = sum(1 for f in audio if f.parent == folder)
        win = focus_program() or win
        time.sleep(0.5)

        # Click the app FIRST so it truly has keyboard focus, then send Ctrl+O with
        # real holds; retry once. If no dialog window appears, we DON'T blind-type.
        dialog = None
        for _ in range(2):
            pyautogui.click(appx, appy)
            time.sleep(0.5)
            before = {getattr(w, "_hWnd", None) for w in gw.getAllWindows()} if gw else set()
            _send_hotkey(pyautogui, IMPORT_HOTKEY)     # open the file dialog
            dialog = _wait_for_dialog(gw, before, timeout=4.0) if gw else None
            if dialog is not None or gw is None:
                break

        if gw is not None and dialog is None:
            print("    [!] no Open dialog appeared after Ctrl+O (tried twice).")
            try:
                titles = sorted({(w.title or "").strip() for w in gw.getAllWindows()
                                 if (w.title or "").strip()})
                print("        windows open right now:")
                for t in titles:
                    print(f"          - {t}")
            except Exception:
                pass
            print("        rekordbox ignored Ctrl+O even as hardware scancodes. Two likely causes:")
            print("        (1) FOCUS: the click landed on a deck/pane where Ctrl+O isn't honored.")
            print("            Try clicking the COLLECTION list instead via TG_IMPORT_LIST_CLICK.")
            print("        (2) rekordbox filters injected keystrokes entirely. If so, keystrokes")
            print("            are a dead end -- ask me to wire up the no-GUI pyrekordbox path.")
            print("        Skipping this folder (won't type into rekordbox by mistake).")
            continue

        if gw is None:
            time.sleep(1.2)                        # can't detect; assume it opened
            listx, listy = appx, appy
        else:
            listx = dialog.left + dialog.width // 2
            listy = dialog.top + dialog.height // 2
            print(f"    dialog opened: '{dialog.title}'  -> list-click ({listx}, {listy})")

        pyautogui.hotkey("ctrl", "a"); pyautogui.press("delete")   # clear the File name box
        pyautogui.write(str(folder) + os.sep, interval=0.01)       # go into the folder
        time.sleep(0.3)
        pyautogui.press("enter")
        time.sleep(1.0)
        pyautogui.click(listx, listy)             # focus the file list
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a")             # select every file shown
        time.sleep(0.3)
        if IMPORT_PAUSE:
            print(f"    pausing {IMPORT_PAUSE:g}s -- files should be highlighted. Corner = abort.")
            time.sleep(IMPORT_PAUSE)
        pyautogui.press("enter")                  # import the whole selection
        print(f"    imported folder: {folder}  (~{n} track(s))")
        time.sleep(2.0 + 0.03 * n)                # let rekordbox ingest them
    print("    done.")


def run_import(files):
    """Import `files` using whichever method IMPORT_MODE selects."""
    if IMPORT_MODE == "file":
        for f in files:
            import_into_program(f)
    else:
        import_files_bulk(files)


def import_existing_folder():
    """--import : import songs already sitting in DOWNLOAD_DIR (no Telegram needed)."""
    files = sorted(
        Path(p) for p in DOWNLOAD_DIR.glob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    )
    if not files:
        print(f"No audio files found in {DOWNLOAD_DIR}.")
        return
    if not (PROGRAM_WINDOW_TITLE or PROGRAM_EXE):
        print(f"Found {len(files)} importable file(s) in {DOWNLOAD_DIR}, but no target set.")
        print('Set TG_PROGRAM_TITLE="rekordbox" first so I know which window to drive.')
        return
    if IMPORT_LIMIT and IMPORT_LIMIT < len(files):
        if IMPORT_MODE == "file":
            print(f"(dry run: importing only the first {IMPORT_LIMIT} of {len(files)} files)")
            files = files[:IMPORT_LIMIT]
        else:
            print(f"(note: folder mode selects the whole folder via Ctrl+A, so "
                  f"TG_IMPORT_LIMIT is ignored here -- use TG_IMPORT_PAUSE to preview, "
                  f"or TG_IMPORT_MODE=file to cap the count)")
    print(f"Importing from {DOWNLOAD_DIR} into '{PROGRAM_WINDOW_TITLE}' ...")
    print("Keep hands off the mouse/keyboard. Emergency stop: shove the mouse to a screen corner.")
    run_import(files)


# =====================================================================
# Wiring
# =====================================================================

def get_links():
    cli = [a for a in sys.argv[1:] if not a.startswith("--")]
    if cli:
        return cli
    lf = Path("links.txt")
    if lf.exists():
        return [
            ln.strip() for ln in lf.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
    entered = input("Paste Spotify link(s), separated by spaces: ").strip()
    return entered.split() if entered else []


def check_config():
    problems = []
    if not API_ID or not API_HASH:
        problems.append("TG_API_ID / TG_API_HASH not set (get them at https://my.telegram.org).")
    if BOT_USERNAME.startswith("@Your"):
        problems.append("BOT_USERNAME still points at the placeholder bot.")
    if problems:
        print("Config still needs attention:")
        for p in problems:
            print("  -", p)
        print()
    return not problems


def print_report(results):
    """Print an end-of-run summary and save it next to the songs."""
    downloaded, skipped, failed = results["downloaded"], results["skipped"], results["failed"]
    all_names = downloaded + skipped + [n for n, _ in failed]
    unique = sorted(set(all_names))
    dupes = len(all_names) - len(unique)

    lines = [
        "",
        "==================== DOWNLOAD REPORT ====================",
        f"  Tracks seen         : {len(all_names)}",
        f"    downloaded now    : {len(downloaded)}",
        f"    already had       : {len(skipped)}",
        f"    failed            : {len(failed)}",
        f"  Unique tracks       : {len(unique)}",
    ]
    if dupes:
        lines.append(f"  Duplicate sends     : {dupes}")
    lines.append(f"  Folder              : {DOWNLOAD_DIR}")
    if failed:
        lines.append("")
        lines.append("  FAILED (rerun the same command to retry just these):")
        for n, err in failed:
            lines.append(f"    - {n}  ({err})")
    lines.append("========================================================")
    report = "\n".join(lines)
    print(report)

    try:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        report_path = DOWNLOAD_DIR / "_download_report.txt"
        full = report + "\n\nAll tracks:\n" + "\n".join(f"  {n}" for n in unique) + "\n"
        report_path.write_text(full, encoding="utf-8")
        print(f"  (full report saved to {report_path})")
    except Exception as e:
        print(f"  [!] could not save report file: {e}")


async def main():
    if "--import" in sys.argv:
        # Import files already on disk; no Telegram connection needed.
        import_existing_folder()
        return

    history_mode = "--history" in sys.argv
    links = [] if history_mode else get_links()
    if not history_mode and not links:
        print("No links given. Pass them as arguments, put them in links.txt,")
        print("or use  --history  to grab files the bot has already sent.")
        return
    if not check_config():
        print("Fix the items above, then rerun.")
        return

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()  # first run: prompts for phone + code
    bot = await client.get_entity(BOT_USERNAME)

    results = new_results()
    if history_mode:
        await grab_history(client, bot, HISTORY_LIMIT, results)
    else:
        print(f"Sending {len(links)} link(s); downloading up to {DOWNLOAD_CONCURRENCY} song(s) at a time ...")
        await grab_all(client, bot, links, results)

    await client.disconnect()

    print_report(results)

    all_files = results["files"]
    if not all_files:
        print("\nNothing downloaded.")
        return

    if PROGRAM_WINDOW_TITLE or PROGRAM_EXE:
        print(f"\nImporting {len(all_files)} file(s) into the program ...")
        print("Keep hands off the mouse/keyboard. (Emergency stop: shove the mouse to a screen corner.)")
        run_import(all_files)
    else:
        print("\n(set TG_PROGRAM_TITLE to auto-import these into rekordbox,")
        print(" then  python tg_music_grabber.py --import  to import what's already downloaded)")


if __name__ == "__main__":
    asyncio.run(main())
