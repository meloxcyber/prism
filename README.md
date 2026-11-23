# PRISM

A clap-to-wake voice assistant for your PC. Clap twice, Prism greets you with the weather in a calm British voice, and you can talk to it or ask it to open any of your web apps. Its brain is **your Claude Code login** — it calls the `claude` CLI, so no API key is needed.

```
👏👏   →  "Good morning, sir. It's 54 degrees with clear skies in Midland.
           Expect a high of 71 and a low of 46. How can I help?"
you    →  "Open the buy desk."
Prism  →  "Opening HOH Buy Desk."            (browser opens it)
you    →  "What's a good markup on a $90 pair that sells for $140 on StockX?"
Prism  →  (asks Claude, answers in one or two calm sentences)
you    →  "That's all."
Prism  →  "Very good. Standing by."           (back to listening for claps)
```

**Console (hosted on Vercel):** [prism-meloxcyber.vercel.app](https://prism-meloxcyber.vercel.app) — launcher grid, voice cheat-sheet, and the live `apps.json` the desktop app syncs from.
**Source:** [github.com/meloxcyber/prism](https://github.com/meloxcyber/prism)

## Install (once)

1. **Python 3.10+** with "Add to PATH" ticked, and **Git for Windows**.
2. **Claude Code** installed and signed in. `setup_brain.bat` installs it and walks you through sign-in.
3. `git clone https://github.com/meloxcyber/prism` (or use the folder you already have) and double-click `install.bat`.

Python packages go in `%LOCALAPPDATA%\PRISM\venv`, not in the repo/OneDrive folder.

## Updates

- **Code**: `PRISM.bat` runs `git pull` on every launch (skip with `PRISM.bat --no-update`). If the folder isn't a git checkout yet, the first launch links it to GitHub automatically.
- **App list**: on start-up the assistant fetches `apps.json` from the hosted console (`sync.apps_url` in config), so apps added via GitHub reach every PC without touching files.
- **Your local settings never get overwritten**: `config.json` and `apps.local.json` are git-ignored. Copy `config.local.example.json` / `apps.local.example.json` to start them.

## The HUD

When PRISM starts it opens a JARVIS-style HUD in a chromeless Edge window (`http://127.0.0.1:7777`):

- **Arc reactor** in the centre that changes with Prism's state: dim cyan *standing by*, bright cyan *listening*, amber *processing*, white *speaking*. The ring reacts to your voice live.
- **Systems**: brain (Claude Code), mic, voice, wake detector, hearing, weather. A green or red dot for each.
- **Acoustic sensor**: live mic scope with the clap threshold line. Every clap flashes **CLAP**; a double clap wakes Prism. Use it to tune `wake.clap_threshold`.
- **Comms log**: everything said, both ways.
- **Mic on/off**: the switch on the Acoustics panel, the **Mic** button, or press **M**. Off means the audio stream is stopped completely and Windows' mic indicator goes dark; wake Prism from the HUD or by typing instead. You can also say *"mute the mic"*.
- **Clap threshold − / +**: raise it if background noise counts as claps, lower it if your claps get missed. It saves to your `config.json`.
- **Command line + buttons**: type a command, **Wake**, **Test voice**, **Weather**.
- **Launch bar**: Vercel projects and Claude artifacts, one click each.

Close the window any time; reopen it at `http://127.0.0.1:7777`. `hud.open_on_start: false` stops it opening automatically, and `PRISM.bat --no-hud` turns it off.

## Run

Double-click `PRISM.bat`. Clap twice to wake it. Press **Enter** in the console to wake it without clapping, or type a command and press Enter to test without the microphone.

| Command | What it does |
|---|---|
| `PRISM.bat` | normal mode |
| `PRISM.bat --calibrate` | 20-second live mic meter; suggests a `clap_threshold` |
| `PRISM.bat --devices` | lists microphones (set `listen.input_device` to an index) |
| `PRISM.bat --say "text"` | tests the voice |
| `PRISM.bat --text` | keyboard-only mode, no mic |
| `autostart.bat` | launches Prism minimised at Windows sign-in (`autostart.bat remove` to undo) |

## What it does locally vs. via Claude

Fast, offline-ish paths (no Claude round-trip):
- **"open / pull up / show me the ___"** → fuzzy-matched against `apps.json`
- **weather / forecast** → Open-Meteo (free, no key; location auto-detected by IP, falls back to Midland)
- **what time is it / what's the date**
- **what can you open**
- **goodbye / that's all / go to sleep** → back to clap-listening

Everything else goes to `claude -p` with a system prompt that includes the time, weather, your app list and the last few turns of conversation. Claude replies as Prism in one or two sentences and can also return app keys to open ("open everything I need for a buy-in" → Buy Desk + Buy-In Board). Tools are disabled for those calls, so Prism can't edit files or run commands through Claude. A Claude round-trip takes a few seconds — that's CLI start-up, not Prism.

## Store tasks

| Say | Prism does |
|---|---|
| "What's on my list today?" | Reads your three tasks with live numbers |
| "Run the missed sales report" | Last 7 days → PDF in `JXRS\Reports` → opens it → reads the headline |
| "How many missed sales this week?" | Count and the top item, no files |
| "How many items need cost?" | Counts store-owned ConsignR items with no cost (same rule as Cost Fixer) |
| "Open the cost fixer" | Opens Cost Fixer and says the count |
| "Open the liquidation tool" | HOH Liquidation + StockX Pro History → Completed + eBay Seller Hub → All orders |
| "Run all my store tasks" | All of the above in order |

The full list lives in `commands.json`. Press **C** on the HUD, or click **Commands**, to see every phrase; click any phrase to run it. The **Store tasks** tab on the launch bar has one-click buttons.

Passwords go in your local `config.json` only (git-ignored):

```json
"store": { "missed_sales_key": "…", "cost_fixer_password": "…" }
```

## The brain (Claude Code)

Prism thinks with **Claude Code**, signed in with your Claude account (no API key). Click **Connect brain** on the HUD, or double-click `setup_brain.bat`. It installs Claude Code with the official installer if it's missing, then opens your browser to sign in. Prism notices within seconds; the BRAIN light turns green with no restart.

By default the brain can use **web search and web fetch**, so it can answer about the present (news, prices, scores, hours). It can't run commands or edit files. To widen that, set `brain.tools` in `config.json`: add `"Read","Glob","Grep"` to read files, and only add `"Bash"`/`"Write"`/`"Edit"` if you want a voice command to be able to change your PC. `brain.model` (e.g. `"haiku"`) trades depth for speed.

## Add your own apps

Edit `apps.json`. Three kinds of target:

```json
{ "key": "shopify",   "name": "Shopify Admin", "url": "https://admin.shopify.com/store/your-store" },
{ "key": "pos",       "name": "Point of Sale", "type": "path",    "path": "C:\\Users\\blais\\Desktop\\POS.lnk" },
{ "key": "dev-site",  "name": "Dev Server",    "type": "command", "command": "cmd /c start http://localhost:3000" }
```

Add `"aliases": ["..."]` for other ways you'd say it. Shared apps live in `apps.json` (edit on GitHub or ask Claude); PC-only apps (shortcuts, local servers) go in `apps.local.json`. It's pre-loaded with the 29 Claude web apps you've built (Deck Lab, Duel Night Records, Midland Hub, Buy Desk, Drop Calendar, …).

## Repo layout

```
prism.py                 desktop assistant
config.example.json      shipped defaults   ->  config.json      (yours, git-ignored)
apps.json                shared registry    ->  apps.local.json  (yours, git-ignored)
install.bat / PRISM.bat / autostart.bat
web/index.html           the console (static)
scripts/build-web.mjs    copies web/ + apps.json + meta.json into dist/ for Vercel
vercel.json, package.json
```

## Tuning

`config.json` (overrides `config.example.json`):

- **Voice** – `edge_voice`: `en-GB-RyanNeural` (calm male, default), `en-GB-ThomasNeural`, `en-GB-SoniaNeural` or `en-GB-LibbyNeural` (female). `rate` like `-6%` slows it down. The neural voice needs internet; offline it falls back to Windows' built-in British voice (Hazel/George, if installed).
- **Clap** – `clap_threshold` 0.15–0.35. Lower = more sensitive. Run `--calibrate` in your normal room noise and clap from where you'd usually sit. `min_gap_ms`/`max_gap_ms` bound the gap between the two claps.
- **Listening** – `followup_window_s`: how long Prism keeps listening after an answer before dozing off. `stt_engine: "whisper"` (after `pip install faster-whisper` into the venv) makes speech recognition fully offline; the default `google` recogniser is free but online.
- **Greeting** – `address_user_as` ("sir", your name, …). Full weather greeting on the first wake and whenever it's been `regreet_after_minutes` since you last spoke; otherwise just "Yes, sir?".
- **Brain** – `claude_path` if `claude` isn't on PATH; `model` to pin one (e.g. `haiku` for faster, cheaper replies).

## Troubleshooting

- **Claps not detected** → `--calibrate`; make sure the right mic is default (`--devices`); try `clap_threshold: 0.15`.
- **Wakes on its own** → raise `clap_threshold`; keep the mic away from speakers.
- **"My brain isn't signed in"** → open a terminal, run `claude`, complete login.
- **No voice** → press **Test voice** on the HUD and check the Voice row in Systems. The neural voice needs internet; offline it falls back to Windows' built-in voices. Playback uses Windows' own audio player, so there's no pygame to install.
- **Speech not recognised** → the Google recogniser needs internet; or switch to whisper (offline).
