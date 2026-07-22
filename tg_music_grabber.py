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

# --- Destination program (GUI automation) -------------------------------------
# The window title is matched as a substring, e.g. "rekordbox", "Serato",
# "iTunes", "Plex". PROGRAM_EXE is optional: launched only if the window
# isn't already open. Leave PROGRAM_WINDOW_TITLE empty to skip auto-import
# (files are just downloaded and listed).
PROGRAM_WINDOW_TITLE = os.environ.get("TG_PROGRAM_TITLE", "")     # <-- FILL IN
PROGRAM_EXE = os.environ.get("TG_PROGRAM_EXE", "")               # optional
IMPORT_HOTKEY = tuple(os.environ.get("TG_IMPORT_HOTKEY", "ctrl,o").split(","))

AUDIO_EXTS = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus", ".aac")

# =====================================================================
# Telegram side
# =====================================================================

def is_audio_message(msg) -> bool:
    """True if this bot message carries a song we should download."""
    if getattr(msg, "audio", None) or getattr(msg, "voice", None):
        return True
    doc = getattr(msg, "document", None)
    if doc:
        if doc.mime_type and doc.mime_type.startswith("audio"):
            return True
        for attr in doc.attributes:
            name = getattr(attr, "file_name", "") or ""
            if name.lower().endswith(AUDIO_EXTS):
                return True
    return False


async def maybe_click_button(msg, pattern: str) -> bool:
    """Click the best-matching inline button on a message. Returns True if clicked."""
    if not msg.buttons:
        return False
    buttons = [b for row in msg.buttons for b in row]
    target = None
    if pattern:
        rx = re.compile(pattern, re.I)
        target = next((b for b in buttons if b.text and rx.search(b.text)), None)
    if target is None and AUTO_CLICK_FIRST_BUTTON:
        target = buttons[0]
    if target is None:
        return False
    print(f"    clicking button: {target.text!r}")
    try:
        await target.click()
        return True
    except Exception as e:
        print(f"    [!] button click failed: {e}")
        return False


async def grab_song(client: TelegramClient, bot, url: str):
    """Send one link, handle buttons, download whatever audio the bot returns."""
    downloaded = []
    queue: asyncio.Queue = asyncio.Queue()
    seen = set()          # (msg id, edit_date) already processed
    clicked_msgs = set()  # msg ids whose buttons we've already clicked

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

            if is_audio_message(msg):
                DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
                path = await client.download_media(msg, file=str(DOWNLOAD_DIR) + os.sep)
                if path:
                    print(f"    downloaded: {path}")
                    downloaded.append(Path(path))
            elif msg.buttons and msg.id not in clicked_msgs:
                if await maybe_click_button(msg, BUTTON_MATCH):
                    clicked_msgs.add(msg.id)
            elif msg.message:
                print(f"    bot: {msg.message[:100]}")
    finally:
        client.remove_event_handler(handler)

    return downloaded


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
    if len(sys.argv) > 1:
        return sys.argv[1:]
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


async def main():
    links = get_links()
    if not links:
        print("No Spotify links given. Pass them as arguments or put them in links.txt.")
        return
    if not check_config():
        print("Fix the items above, then rerun.")
        return

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()  # first run: prompts for phone + code
    bot = await client.get_entity(BOT_USERNAME)

    all_files = []
    for url in links:
        print(f"\n=== {url} ===")
        files = await grab_song(client, bot, url)
        if files:
            all_files.extend(files)
        else:
            print("    no audio received (check the bot / button match).")

    await client.disconnect()

    if not all_files:
        print("\nNothing downloaded.")
        return

    if PROGRAM_WINDOW_TITLE or PROGRAM_EXE:
        print(f"\nImporting {len(all_files)} file(s) into the program ...")
        print("Keep hands off the mouse/keyboard. (Emergency stop: shove the mouse to a screen corner.)")
        for f in all_files:
            import_into_program(f)
    else:
        print("\nDownloaded (set TG_PROGRAM_TITLE to auto-import these):")
        for f in all_files:
            print("  ", f)


if __name__ == "__main__":
    asyncio.run(main())
