#!/usr/bin/env python3
"""
Telegram Spotify-song grabber  ->  import into a desktop program.

Pipeline (one link at a time):
  1. Send a Spotify link to a Telegram music bot, using YOUR user account (Telethon).
  2. Click the bot's inline button(s) (quality / download), if it shows any.
  3. Download the audio file(s) the bot sends back into DOWNLOAD_DIR.
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
IDLE_TIMEOUT = int(os.environ.get("TG_IDLE_TIMEOUT", "45"))       # wait per next msg
OVERALL_TIMEOUT = int(os.environ.get("TG_OVERALL_TIMEOUT", "300"))  # hard cap per link
HISTORY_LIMIT = int(os.environ.get("TG_HISTORY_LIMIT", "200"))     # --history: msgs to scan

# --- Destination program (GUI automation) -------------------------------------
# The window title is matched as a substring, e.g. "rekordbox", "Serato",
# "iTunes", "Plex". PROGRAM_EXE is optional: launched only if the window
# isn't already open. Leave PROGRAM_WINDOW_TITLE empty to skip auto-import
# (files are just downloaded and listed).
PROGRAM_WINDOW_TITLE = os.environ.get("TG_PROGRAM_TITLE", "")     # <-- FILL IN
PROGRAM_EXE = os.environ.get("TG_PROGRAM_EXE", "")               # optional
IMPORT_HOTKEY = tuple(os.environ.get("TG_IMPORT_HOTKEY", "ctrl,o").split(","))

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


async def download_one(client, msg, results):
    """Download one song/archive message; record the outcome in `results`.

    Skips a file we already have whole, re-fetches partial files, and never lets
    one failed download abort the whole batch.
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
        path = await client.download_media(msg, file=dest)
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


async def grab_song(client: TelegramClient, bot, url: str, results):
    """Send one link, handle buttons, download whatever audio the bot returns."""
    queue: asyncio.Queue = asyncio.Queue()
    seen = set()             # (msg id, edit_date) states already processed
    clicked_buttons = set()  # (msg id, button text) we've already clicked

    async def handler(event):
        await queue.put(event.message)

    client.add_event_handler(handler, events.NewMessage(from_users=bot))
    client.add_event_handler(handler, events.MessageEdited(from_users=bot))

    try:
        await client.send_message(bot, url)
        loop = asyncio.get_event_loop()
        start = loop.time()
        while True:
            remaining = OVERALL_TIMEOUT - (loop.time() - start)
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=min(IDLE_TIMEOUT, remaining))
            except asyncio.TimeoutError:
                break  # bot went quiet -> assume it's done

            key = (msg.id, getattr(msg, "edit_date", None))
            if key in seen:
                continue
            seen.add(key)

            if is_wanted_media(msg):
                await download_one(client, msg, results)
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


async def grab_history(client, bot, limit, results):
    """Download files the bot has ALREADY sent into the chat (no re-triggering)."""
    print(f"Scanning the last {limit} messages from the bot for songs ...")
    async for msg in client.iter_messages(bot, limit=limit):
        if is_wanted_media(msg):
            await download_one(client, msg, results)


# =====================================================================
# Desktop-program side (GUI automation)
# =====================================================================

def focus_program() -> bool:
    """Bring the destination program to the foreground (launching it if needed)."""
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

    w = wins[0]
    try:
        if getattr(w, "isMinimized", False):
            w.restore()
        w.activate()
    except Exception:
        # activate() is flaky on Windows; a click usually still lands on the app
        pass
    time.sleep(0.6)
    return True


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

    pyautogui.FAILSAFE = True  # slam mouse to a screen corner to abort
    if not focus_program():
        print(f"    [!] skipping import of {file_path.name} (program not focused).")
        return

    time.sleep(1.0)
    pyautogui.hotkey(*IMPORT_HOTKEY)     # open the file dialog
    time.sleep(1.5)
    pyautogui.write(str(file_path), interval=0.02)
    time.sleep(0.4)
    pyautogui.press("enter")
    time.sleep(1.5)
    print(f"    imported into program: {file_path.name}")


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
        for url in links:
            print(f"\n=== {url} ===")
            await grab_song(client, bot, url, results)

    await client.disconnect()

    print_report(results)

    all_files = results["files"]
    if not all_files:
        print("\nNothing downloaded.")
        return

    if PROGRAM_WINDOW_TITLE or PROGRAM_EXE:
        print(f"\nImporting {len(all_files)} file(s) into the program ...")
        print("Keep hands off the mouse/keyboard. (Emergency stop: shove the mouse to a screen corner.)")
        for f in all_files:
            import_into_program(f)
    else:
        print("\n(set TG_PROGRAM_TITLE to auto-import these into rekordbox)")


if __name__ == "__main__":
    asyncio.run(main())
