#!/usr/bin/env python3
"""Figure out why the rekordbox import did nothing.

Run this in the SAME shell you run tg_music_grabber.py in, with rekordbox open:

    $env:TG_PROGRAM_TITLE = "rekordbox"
    python diagnose_import.py
"""
import sys
from pathlib import Path

print("=" * 64)
print("IMPORT DIAGNOSTICS")
print("=" * 64)
print(f"python: {sys.executable}")

ok = True

# 1) GUI libraries -- these are what actually drive the import.
try:
    import pyautogui
    sw, sh = pyautogui.size()
    print(f"[OK]   pyautogui {getattr(pyautogui, '__version__', '?')}  (screen {sw}x{sh})")
except Exception as e:
    ok = False
    sw = sh = 0
    print(f"[FAIL] pyautogui NOT importable: {e}")

try:
    import pygetwindow as gw
    print("[OK]   pygetwindow importable")
except Exception as e:
    ok = False
    gw = None
    print(f"[FAIL] pygetwindow NOT importable: {e}")

if not ok:
    print()
    print(">>> FIX: install the GUI libs into THIS python, then retry:")
    print("        python -m pip install -r requirements.txt")

# 2) Config / env as the script sees it.
try:
    import tg_music_grabber as g
except Exception as e:
    print(f"[FAIL] cannot import tg_music_grabber: {e}")
    sys.exit(1)

print()
print(f"TG_PROGRAM_TITLE = {g.PROGRAM_WINDOW_TITLE!r}")
if not (g.PROGRAM_WINDOW_TITLE or g.PROGRAM_EXE):
    print("       [!] EMPTY -> import is skipped entirely. In THIS shell run:")
    print('           $env:TG_PROGRAM_TITLE = "rekordbox"')
print(f"IMPORT_MODE      = {g.IMPORT_MODE}")
print(f"IMPORT_LIST_CLICK= {g.IMPORT_LIST_CLICK}")
print(f"DOWNLOAD_DIR     = {g.DOWNLOAD_DIR}")

# 3) Are the songs actually there?
if g.DOWNLOAD_DIR.exists():
    audio = [p for p in g.DOWNLOAD_DIR.glob("*")
             if p.is_file() and p.suffix.lower() in g.AUDIO_EXTS]
    print(f"       audio files found: {len(audio)}")
    if audio:
        print(f"         e.g. {audio[0].name}")
    else:
        print("       [!] no audio files here -> nothing to import.")
else:
    print("       [!] this folder does NOT exist.")

# 4) Can we see the rekordbox window?
if gw is not None:
    title = g.PROGRAM_WINDOW_TITLE or "rekordbox"
    wins = gw.getWindowsWithTitle(title)
    print()
    print(f"Windows whose title contains {title!r}: {len(wins)}")
    for w in wins[:5]:
        print(f"   - {w.title!r}  at ({w.left},{w.top})  {w.width}x{w.height}")
    if not wins:
        print("   [!] rekordbox NOT found by that title. Open windows right now:")
        for w in gw.getAllWindows():
            t = (w.title or "").strip()
            if t:
                print(f"         {t!r}")

# 5) Where the list-click would land.
if ok:
    cx, cy = int(sw * g.IMPORT_LIST_CLICK[0]), int(sh * g.IMPORT_LIST_CLICK[1])
    print()
    print(f"List-click would happen at ({cx}, {cy}) on the primary monitor.")

print("=" * 64)
print("Paste this whole output back.")
