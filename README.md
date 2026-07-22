# Telegram Spotify-song grabber → desktop program

Pastes a Spotify link into a Telegram music bot, clicks the bot's buttons,
downloads the song(s) it sends back, and GUI-imports each file into another
PC program.

- **Telegram side:** Telethon (drives your *user* account programmatically — no on-screen clicking, works even if Telegram is closed).
- **Desktop side:** GUI automation (focus the app → Ctrl+O → type path → Enter).

## 1. Install

```powershell
cd telegram-song-grabber
python -m pip install -r requirements.txt
```

## 2. Get Telegram API credentials (once)

1. Go to https://my.telegram.org → **API development tools**.
2. Create an app; copy the **api_id** (number) and **api_hash** (hex string).

Set them (either edit the CONFIG block in `tg_music_grabber.py`, or use env vars):

```powershell
$env:TG_API_ID   = "123456"
$env:TG_API_HASH = "your_api_hash_here"
$env:TG_BOT      = "@TheSpotifyBotYouUse"     # the bot you paste links to
```

## 3. Point it at your destination program

Set the window title (matched as a substring) and, optionally, the exe to launch:

```powershell
$env:TG_PROGRAM_TITLE = "rekordbox"          # e.g. rekordbox / Serato / iTunes / Plex
$env:TG_PROGRAM_EXE   = "C:\Path\To\App.exe" # optional; only used if not already open
```

Leave `TG_PROGRAM_TITLE` empty to just download the songs (no auto-import).

## 4. Run

```powershell
python tg_music_grabber.py "https://open.spotify.com/track/xxxx"
# or list several links in links.txt and just run:
python tg_music_grabber.py
```

First run asks for your phone number + the login code Telegram sends you. After
that a local `tg_music_grabber.session` file keeps you logged in.

During the import step, **keep your hands off the mouse/keyboard**. Emergency
stop: shove the mouse into any screen corner (PyAutoGUI failsafe).

## Two things still worth tuning to your exact setup

1. **The bot's button flow.** `BUTTON_MATCH` (env `TG_BUTTON_MATCH`) is a regex
   matched against button text, e.g. `320|high|mp3|download`. If the bot needs a
   different sequence of taps, tell me its exact reply/buttons and I'll hard-code it.
2. **The import method.** The default `import_into_program()` uses Ctrl+O. If your
   program imports via a menu, a drag-and-drop, or a "watch folder" instead, name
   the program and I'll swap in the exact steps.

## Notes

- Automating a *user* account against bots can bump into Telegram's ToS if done
  aggressively — this script waits on the bot rather than spamming, so keep the
  volume reasonable and you'll be fine.
- Only grab music you're allowed to (your own uploads, licensed, or public domain).
