# Telegram Spotify-song grabber → desktop program

Pastes a Spotify link into a Telegram music bot, clicks the bot's buttons,
downloads the song(s) it sends back, and GUI-imports each file into another
PC program.

- **Telegram side:** Telethon (drives your *user* account programmatically — no on-screen clicking, works even if Telegram is closed).
- **Desktop side:** GUI automation (focus the app → Ctrl+O → type path → Enter).

## 1. Install

```powershell
cd rekordbox-automator
python -m pip install -r requirements.txt
```

## 2. Get Telegram API credentials (once)

1. Go to https://my.telegram.org → **API development tools**.
2. Create an app; copy the **api_id** (number) and **api_hash** (hex string).

Set them via a `.env` file next to `tg_music_grabber.py` (git-ignored, loaded
automatically on startup) — copy this template and fill in your own values:

```dotenv
TG_API_ID=your_api_id_here
TG_API_HASH=your_api_hash_here
TG_BOT=@YourMusicDownloaderBot     # the bot you paste links to
```

Or set them as environment variables for a single session (these override `.env`):

```powershell
$env:TG_API_ID   = "your_api_id_here"
$env:TG_API_HASH = "your_api_hash_here"
$env:TG_BOT      = "@YourMusicDownloaderBot"
```

## 3. Point it at your destination program

Set the window title (matched as a substring) and, optionally, the exe to launch:

```powershell
$env:TG_PROGRAM_TITLE = "rekordbox"          # e.g. rekordbox / Serato / iTunes / Plex
$env:TG_PROGRAM_EXE   = "C:\Path\To\App.exe" # optional; only used if not already open
```

Leave `TG_PROGRAM_TITLE` empty to just download the songs (no auto-import).

### Importing the songs (bulk)

After a download run, the tracks are auto-imported. You can also import whatever is
**already** in the download folder, without touching Telegram:

```powershell
python tg_music_grabber.py --import
```

Import brings in the **whole folder in one dialog**: it opens the file dialog
(Ctrl+O), navigates into the folder, clicks the file list, presses **Ctrl+A** to
select everything, and hits Enter. rekordbox ignores any non-audio files. Knobs:

```powershell
$env:TG_IMPORT_PAUSE      = "5"        # hold 5s with files selected BEFORE Enter, so you can look
$env:TG_IMPORT_LIST_CLICK = "0.5,0.42" # where to click to focus the list (screen fractions)
$env:TG_IMPORT_MODE       = "file"     # fall back to one dialog per track if select-all misbehaves
```

**First run — preview before it commits:** with rekordbox open, set
`TG_IMPORT_PAUSE=5` and run `--import`. It selects all the files and pauses 5s so you
can confirm they're highlighted in the dialog. If nothing is highlighted, the click
missed the list — shove the mouse into a screen corner to abort, adjust
`TG_IMPORT_LIST_CLICK`, and retry. Once it looks right, unset the pause and it'll
import instantly. (The mouse-corner failsafe aborts at any time.)

## 4. Run

```powershell
python tg_music_grabber.py "https://open.spotify.com/track/xxxx"
# or list several links in links.txt and just run:
python tg_music_grabber.py
```

First run asks for your phone number + the login code Telegram sends you. After
that a local `tg_music_grabber.session` file keeps you logged in.

All the links are sent up front and the songs the bot returns download **in
parallel** (not one-at-a-time), so a batch finishes much faster. The concurrency
is deliberately *bounded* — hammering one Telegram connection with dozens of
simultaneous downloads just trips flood limits and ends up slower. Tune it:

```powershell
$env:TG_DOWNLOAD_CONCURRENCY = "4"    # songs downloading at once (default 4)
$env:TG_SEND_STAGGER         = "1.0"  # seconds between sending each link (default 1.0)
```

If Telegram asks the script to wait (flood control), it backs off and retries
automatically. `--history` downloads run in parallel the same way.

**It waits for the bot to actually finish.** The script keeps running as long as
the bot is still working — a track still downloading, or the bot showing the
"sending…" / "uploading…" indicator all count as activity. It only decides the
bot is done after it goes *completely* silent for `TG_IDLE_TIMEOUT` seconds. If
your bot is a slow one that pauses a long time between tracks, raise it:

```powershell
$env:TG_IDLE_TIMEOUT  = "90"    # silence (no msg, no "sending…") => bot is done (default 90)
$env:TG_STALL_TIMEOUT = "600"   # give up only if NOTHING happens this long (default 600)
```

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
