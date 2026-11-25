#!/usr/bin/env python3
"""
PRISM — a clap-to-wake desktop voice assistant.

  • Wakes when you clap twice
  • Greets you with the local weather in a calm British voice
  • Uses your Claude Code login (the `claude` CLI) as its brain
  • Opens any web app registered in apps.json by voice

Run:  python prism.py            (normal)
      python prism.py --calibrate  (live mic meter to tune clap threshold)
      python prism.py --devices    (list microphones)
      python prism.py --say "Hello"  (test the voice)
      python prism.py --text       (keyboard-only mode, no microphone)
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"              # your local settings (git-ignored)
CONFIG_EXAMPLE = HERE / "config.example.json"   # defaults shipped with the repo
APPS_PATH = HERE / "apps.json"                  # shared registry (updated via GitHub/Vercel)
APPS_LOCAL = HERE / "apps.local.json"           # your personal additions (git-ignored)
APPS_CACHE = HERE / "apps.cache.json"           # last copy synced from Vercel (git-ignored)
COMMANDS_PATH = HERE / "commands.json"          # voice commands: matching + the HUD cheat sheet
IS_WINDOWS = os.name == "nt"
SAMPLE_RATE = 16000
BLOCK_MS = 20
BLOCK = SAMPLE_RATE * BLOCK_MS // 1000

# --------------------------------------------------------------------------- #
# Console
# --------------------------------------------------------------------------- #
try:
    from rich.console import Console

    _console = Console()

    def log(msg: str, style: str = "") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        _console.print(f"[dim]{stamp}[/dim] {msg}", style=style or None, highlight=False)

except Exception:  # rich not installed

    _TAG_RE = re.compile(r"\[/?[a-z ]+\]")

    def log(msg: str, style: str = "") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(stamp + " " + _TAG_RE.sub("", msg))


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: dict = {
    "assistant_name": "Prism",
    "address_user_as": "sir",
    "regreet_after_minutes": 60,
    "voice": {
        "engine": "edge",
        "edge_voice": "en-GB-RyanNeural",
        "rate": "-6%",
        "pitch": "-2Hz",
        "fallback_sapi_voice": "Hazel",
    },
    "wake": {
        "clap_threshold": 0.22,
        "min_gap_ms": 150,
        "max_gap_ms": 800,
        "cooldown_s": 2.0,
    },
    "listen": {
        "input_device": None,
        "stt_engine": "google",
        "language": "en-US",
        "max_seconds": 10,
        "silence_end_s": 1.1,
        "no_speech_timeout_s": 5,
        "followup_window_s": 7,
        "whisper_model": "base.en",
    },
    "weather": {
        "auto_locate": True,
        "city": "Midland, Texas",
        "latitude": 31.9973,
        "longitude": -102.0779,
        "refresh_minutes": 15,
    },
    "brain": {
        "claude_path": "",
        "model": "",
        "timeout_s": 120,
        "history_turns": 8,
        "max_turns": 6,
        # Claude Code tools Prism may use. Add "Read","Glob","Grep" to let it read files;
        # "Bash"/"Write"/"Edit" would let it change your PC — only add them deliberately.
        "tools": ["WebSearch", "WebFetch"],
    },
    "hud": {
        "enabled": True,
        "port": 7777,
        "open_on_start": True,
        "app_window": True,  # chromeless Edge/Chrome window with its own taskbar button
        "tray": True,        # PRISM icon by the clock: click = show HUD, right-click = menu
        "hotkey": True,      # Ctrl+Alt+P brings the HUD back from anywhere
    },
    "store": {
        # Handful of Hype store tasks. Put the two passwords in your local config.json
        # (git-ignored) — never in config.example.json.
        "missed_sales_key": "",
        "cost_fixer_password": "",
        "report_days": 7,
        "report_day": "Monday",
    },
    "sync": {
        # Hosted copy of apps.json (the Vercel console). Fetched at start-up so new apps
        # added through GitHub reach this PC without touching any local files.
        "apps_url": "",
        "timeout_s": 4,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_json(path: Path) -> dict | list | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log(f"[yellow]Could not read {path.name}:[/yellow] {e}")
        return None


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    for p in (CONFIG_EXAMPLE, CONFIG_PATH):  # example first, then your local overrides
        data = _read_json(p)
        if isinstance(data, dict):
            cfg = deep_merge(cfg, data)
    return cfg


def _apps_from(data) -> list[dict]:
    apps = data.get("apps", data) if isinstance(data, dict) else (data or [])
    return [a for a in apps if isinstance(a, dict) and a.get("key")]


def load_apps(sync_cfg: dict | None = None) -> list[dict]:
    apps: list[dict] = []
    url = (sync_cfg or {}).get("apps_url", "")
    if url:
        try:
            remote = _get_json(url, timeout=float((sync_cfg or {}).get("timeout_s", 4)))
            apps = _apps_from(remote)
            if apps:
                with open(APPS_CACHE, "w", encoding="utf-8") as f:  # cache for offline use (never the tracked apps.json)
                    json.dump(remote, f, indent=2, ensure_ascii=False)
                log(f"[dim]synced {len(apps)} apps from {url}[/dim]")
        except Exception as e:
            log(f"[yellow]App sync failed ({e}); using local apps.json[/yellow]")
    if not apps:
        apps = _apps_from(_read_json(APPS_CACHE)) or _apps_from(_read_json(APPS_PATH))

    # Personal additions/overrides, never touched by updates.
    merged = {a["key"]: a for a in apps}
    for a in _apps_from(_read_json(APPS_LOCAL)):
        merged[a["key"]] = {**merged.get(a["key"], {}), **a}
    return list(merged.values())


# --------------------------------------------------------------------------- #
# Microphone stream (shared by clap detection and speech capture)
# --------------------------------------------------------------------------- #
class Mic:
    def __init__(self, device=None):
        import sounddevice as sd

        self._sd = sd
        self.q: "queue.Queue[np.ndarray]" = queue.Queue()
        self.level = 0.0  # live peak (0..1) for the HUD meter
        try:
            idx = device if device is not None else sd.default.device[0]
            self.name = str(sd.query_devices(idx)["name"])
        except Exception:
            self.name = "default microphone"
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=BLOCK,
            device=device,
            callback=self._cb,
        )

    def _cb(self, indata, frames, t, status):  # noqa: D401
        block = indata[:, 0].copy()
        self.level = float(np.abs(block).max()) / 32768.0 if block.size else 0.0
        self.q.put(block)

        self.enabled = False

    def start(self):
        self.stream.start()
        self.enabled = True

    def set_enabled(self, on: bool):
        """Off = the audio stream is stopped entirely (Windows' mic indicator goes dark)."""
        if on == self.enabled:
            return
        try:
            self.stream.start() if on else self.stream.stop()
        except Exception as e:
            log(f"[red]Mic toggle failed:[/red] {e}")
            return
        self.enabled = on
        self.level = 0.0
        self.flush()

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass

    def flush(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def read(self, timeout=1.0) -> np.ndarray | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None


def block_levels(block: np.ndarray) -> tuple[float, float]:
    x = block.astype(np.float32) / 32768.0
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    return peak, rms


# --------------------------------------------------------------------------- #
# Double-clap detector
# --------------------------------------------------------------------------- #
class ClapDetector:
    """
    A clap = a sudden loud, short transient that decays within ~120 ms.
    Two confirmed claps 150–800 ms apart = wake.
    """

    def __init__(self, cfg: dict):
        self.thresh = float(cfg["clap_threshold"])
        self.min_gap = cfg["min_gap_ms"] / 1000.0
        self.max_gap = cfg["max_gap_ms"] / 1000.0
        self.noise = 0.004
        self.last_peak = 0.0
        self.onsets: deque[float] = deque(maxlen=4)
        self.refractory_until = 0.0
        self.pending = None  # (t0, rms0, blocks_seen, loud_blocks)
        self.clap_count = 0  # every confirmed single clap (shown on the HUD)
        self.last_clap = 0.0

    def reset(self):
        self.last_peak = 0.0
        self.pending = None
        self.onsets.clear()

    def feed(self, block: np.ndarray, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        peak, rms = block_levels(block)
        fired = False

        if self.pending is not None:
            t0, rms0, seen, loud = self.pending
            seen += 1
            if rms > rms0 * 0.35:
                loud += 1
            if seen >= 6:  # 120 ms after onset
                self.pending = None
                if loud <= 2:  # it decayed quickly -> a real clap
                    fired = self._register(t0)
            else:
                self.pending = (t0, rms0, seen, loud)
            self.last_peak = peak
            return fired

        if peak < self.thresh * 0.5:
            self.noise = 0.95 * self.noise + 0.05 * rms

        is_onset = (
            peak >= self.thresh
            and peak > self.last_peak * 2.5
            and rms > self.noise * 6
            and now >= self.refractory_until
        )
        if is_onset:
            self.pending = (now, rms, 0, 0)
            self.refractory_until = now + 0.1
        self.last_peak = peak
        return False

    def _register(self, t: float) -> bool:
        self.clap_count += 1
        self.last_clap = time.time()
        if self.onsets:
            gap = t - self.onsets[-1]
            if self.min_gap <= gap <= self.max_gap:
                self.onsets.clear()
                return True
        self.onsets.append(t)
        return False


# --------------------------------------------------------------------------- #
# Speech capture + transcription
# --------------------------------------------------------------------------- #
def record_utterance(mic: Mic, cfg: dict, noise_rms: float, should_abort=None) -> np.ndarray | None:
    """Capture one spoken phrase. Returns None on silence, timeout, or if should_abort() fires."""
    speech_thresh = max(0.012, noise_rms * 4.0)
    frames: list[np.ndarray] = []
    started = False
    start_idx = 0
    t_start = time.monotonic()
    last_voice = t_start

    while True:
        if should_abort is not None and not started and should_abort():
            return None
        b = mic.read(0.25)
        now = time.monotonic()
        if b is None:
            if not started and now - t_start > cfg["no_speech_timeout_s"]:
                return None
            if started and now - last_voice > cfg["silence_end_s"]:
                break
            continue
        frames.append(b)
        _, rms = block_levels(b)
        if rms > speech_thresh:
            if not started:
                started = True
                start_idx = max(0, len(frames) - 25)  # keep 0.5 s pre-roll
            last_voice = now
        if not started and now - t_start > cfg["no_speech_timeout_s"]:
            return None
        if started and now - last_voice > cfg["silence_end_s"]:
            break
        if now - t_start > cfg["max_seconds"]:
            break

    if not started:
        return None
    return np.concatenate(frames[start_idx:])


class Transcriber:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.engine = cfg.get("stt_engine", "google")
        self._whisper = None

    def transcribe(self, audio: np.ndarray) -> str | None:
        if self.engine == "whisper":
            return self._whisper_tx(audio)
        return self._google_tx(audio)

    def _google_tx(self, audio: np.ndarray) -> str | None:
        import speech_recognition as sr

        r = sr.Recognizer()
        data = sr.AudioData(audio.tobytes(), SAMPLE_RATE, 2)
        try:
            return r.recognize_google(data, language=self.cfg.get("language", "en-US"))
        except sr.UnknownValueError:
            return None
        except sr.RequestError as e:
            log(f"[red]Speech service error:[/red] {e}")
            return None

    def _whisper_tx(self, audio: np.ndarray) -> str | None:
        if self._whisper is None:
            from faster_whisper import WhisperModel

            self._whisper = WhisperModel(self.cfg.get("whisper_model", "base.en"), compute_type="int8")
        segments, _ = self._whisper.transcribe(audio.astype(np.float32) / 32768.0, language="en", vad_filter=True)
        text = " ".join(s.text for s in segments).strip()
        return text or None


# --------------------------------------------------------------------------- #
# Voice (calm British)
# --------------------------------------------------------------------------- #
class Voice:
    """Calm British voice. Neural (edge-tts) when online, Windows SAPI offline.

    MP3 playback on Windows goes through the built-in winmm/MCI player, so there is
    no pygame/SDL dependency to break on newer Python versions.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._lock = threading.Lock()
        self.last_engine = ""
        self.last_error = ""

    def say(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        log(f"[bold cyan]PRISM:[/bold cyan] {text}")
        with self._lock:
            if self.cfg.get("engine", "edge") == "edge":
                try:
                    self._edge(text)
                    self.last_engine, self.last_error = "neural", ""
                    return
                except Exception as e:
                    self.last_error = str(e)[:160]
                    log(f"[yellow]Neural voice unavailable ({e}); using offline voice.[/yellow]")
            try:
                self._sapi(text)
                self.last_engine = "offline"
            except Exception as e:
                self.last_engine, self.last_error = "none", str(e)[:160]
                log(f"[red]No voice output:[/red] {e}")

    def _edge(self, text: str) -> None:
        import edge_tts

        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="prism_")
        os.close(fd)

        async def gen():
            await edge_tts.Communicate(
                text,
                self.cfg.get("edge_voice", "en-GB-RyanNeural"),
                rate=self.cfg.get("rate", "-6%"),
                pitch=self.cfg.get("pitch", "-2Hz"),
            ).save(path)

        try:
            asyncio.run(gen())
            if os.path.getsize(path) < 200:
                raise RuntimeError("empty audio from voice service")
            play_mp3(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _sapi(self, text: str) -> None:
        import pyttsx3

        engine = pyttsx3.init()
        want = self.cfg.get("fallback_sapi_voice", "Hazel").lower()
        for v in engine.getProperty("voices"):
            name = (v.name or "").lower()
            langs = " ".join(str(x) for x in getattr(v, "languages", []) or []).lower()
            if want in name or "en-gb" in langs or "united kingdom" in name or "british" in name:
                engine.setProperty("voice", v.id)
                break
        engine.setProperty("rate", 165)
        engine.say(text)
        engine.runAndWait()


def play_mp3(path: str) -> None:
    if IS_WINDOWS:
        import ctypes

        mci = ctypes.windll.winmm.mciSendStringW  # type: ignore[attr-defined]
        buf = ctypes.create_unicode_buffer(256)
        alias = f"prism{int(time.time() * 1000) % 10_000_000}"
        err = mci(f'open "{path}" type mpegvideo alias {alias}', buf, 255, 0)
        if err:
            raise RuntimeError(f"Windows audio player error {err}")
        try:
            mci(f"play {alias} wait", buf, 255, 0)
        finally:
            mci(f"close {alias}", buf, 255, 0)
        return
    # macOS / Linux
    for player in (["afplay"], ["mpg123", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        if shutil.which(player[0]):
            subprocess.run(player + [path], check=False)
            return
    import pygame  # last resort

    pygame.mixer.init()
    pygame.mixer.music.load(path)
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        time.sleep(0.05)


# --------------------------------------------------------------------------- #
# Weather (Open-Meteo, no API key)
# --------------------------------------------------------------------------- #
WMO = {
    0: "clear skies", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains", 80: "light showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "severe thunderstorms with hail",
}


def _get_json(url: str, timeout: float = 6.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "PRISM/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Weather:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.data: dict | None = None
        self.city = cfg.get("city", "")
        self.lat = cfg.get("latitude")
        self.lon = cfg.get("longitude")
        self.updated = 0.0
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.refresh()
            except Exception as e:
                log(f"[yellow]Weather refresh failed:[/yellow] {e}")
            time.sleep(max(5, self.cfg.get("refresh_minutes", 15)) * 60)

    def _locate(self):
        if not self.cfg.get("auto_locate", True):
            return
        try:
            j = _get_json("http://ip-api.com/json/?fields=status,city,regionName,lat,lon")
            if j.get("status") == "success":
                self.lat, self.lon = j["lat"], j["lon"]
                self.city = f"{j.get('city')}, {j.get('regionName')}"
        except Exception:
            pass  # keep configured fallback

    def refresh(self):
        if self.updated == 0.0:
            self._locate()
        params = {
            "latitude": self.lat,
            "longitude": self.lon,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,is_day",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "auto",
            "forecast_days": 1,
        }
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
        j = _get_json(url)
        with self._lock:
            self.data = j
            self.updated = time.time()

    def summary(self) -> str:
        with self._lock:
            j = self.data
        if not j:
            return "I couldn't reach the weather service just now."
        cur = j.get("current", {})
        day = j.get("daily", {})
        temp = round(cur.get("temperature_2m", 0))
        feels = round(cur.get("apparent_temperature", temp))
        desc = WMO.get(int(cur.get("weather_code", 0)), "mixed conditions")
        hi = round(day.get("temperature_2m_max", [temp])[0])
        lo = round(day.get("temperature_2m_min", [temp])[0])
        rain = day.get("precipitation_probability_max", [0])[0] or 0
        wind = round(cur.get("wind_speed_10m", 0))
        place = self.city.split(",")[0] if self.city else "your area"
        s = f"It's {temp} degrees with {desc} in {place}"
        if abs(feels - temp) >= 4:
            s += f", feeling more like {feels}"
        s += f". Expect a high of {hi} and a low of {lo}"
        if rain >= 50:
            s += f", with a {rain} percent chance of rain"
        elif rain >= 25:
            s += f", and a slight chance of rain"
        if wind >= 20:
            s += f". It's rather windy, around {wind} miles per hour"
        return s + "."

    def short(self) -> str:
        with self._lock:
            j = self.data
        if not j:
            return "weather unavailable"
        cur = j.get("current", {})
        return f"{round(cur.get('temperature_2m', 0))}°F, {WMO.get(int(cur.get('weather_code', 0)), '?')}"


# --------------------------------------------------------------------------- #
# App launcher
# --------------------------------------------------------------------------- #
OPEN_RE = re.compile(
    r"^(?:(?:hey |ok |okay )?prism[, ]+)?(?:please )?(?:can you |could you |would you )?"
    r"(?:open|launch|start|pull up|bring up|show me|show|go to|load)(?: up)?(?: the)?(?: my)?\s+(.+?)"
    r"(?: (?:app|page|dashboard|please|for me))*[.!?]*$",
    re.I,
)


class Launcher:
    def __init__(self, apps: list[dict]):
        self.apps = apps

    def match(self, phrase: str) -> tuple[dict | None, float]:
        p = phrase.lower().strip()
        best, best_score = None, 0.0
        for app in self.apps:
            names = [app.get("name", ""), app["key"].replace("-", " ")] + list(app.get("aliases", []))
            for n in names:
                n = n.lower().strip()
                if not n:
                    continue
                if n == p:
                    score = 1.0
                elif n in p or p in n:
                    score = 0.9
                else:
                    score = difflib.SequenceMatcher(None, n, p).ratio()
                if score > best_score:
                    best, best_score = app, score
        return best, best_score

    def by_key(self, key: str) -> dict | None:
        for app in self.apps:
            if app["key"] == key:
                return app
        return None

    def open(self, app: dict) -> bool:
        kind = app.get("type", "url")
        target = app.get("url") or app.get("path") or app.get("command")
        try:
            if kind == "url":
                webbrowser.open(target)
            elif kind == "path":
                if IS_WINDOWS:
                    os.startfile(target)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen([target])
            elif kind == "command":
                subprocess.Popen(target, shell=True)
            log(f"[green]Opened[/green] {app.get('name')}")
            return True
        except Exception as e:
            log(f"[red]Failed to open {app.get('name')}:[/red] {e}")
            return False

    def catalogue(self) -> str:
        return "\n".join(
            f'  - key="{a["key"]}"  name="{a.get("name", "")}"  [{a.get("section", "other")}]'
            + (f'  ({a["note"]})' if a.get("note") else "")
            for a in self.apps
        )


# --------------------------------------------------------------------------- #
# Brain = Claude Code CLI (`claude -p`), using your existing login
# --------------------------------------------------------------------------- #
class Brain:
    """Claude Code CLI as Prism's brain (uses your Claude login, no API key).

    Tools: web search/fetch are allowed by default so Prism can answer about the
    present (news, prices, scores, opening hours). Anything that changes your
    computer (Bash, Edit, Write…) stays blocked unless you allow it in config.json.
    """

    ALWAYS_BLOCKED = ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Task", "TodoWrite", "KillShell"]

    def __init__(self, cfg: dict, launcher: Launcher, weather: Weather, address: str):
        self.cfg = cfg
        self.launcher = launcher
        self.weather = weather
        self.address = address
        self.history: deque[tuple[str, str]] = deque(maxlen=int(cfg.get("history_turns", 8)))
        self.exe = self._find_claude()
        self.status = {"ok": False, "state": "checking", "detail": "checking…"}
        self._lock = threading.Lock()
        threading.Thread(target=self._health_loop, daemon=True).start()

    # ---- discovery & health ------------------------------------------------
    def _find_claude(self) -> str | None:
        if self.cfg.get("claude_path") and Path(self.cfg["claude_path"]).exists():
            return self.cfg["claude_path"]
        home = Path.home()
        candidates = [
            home / ".local" / "bin" / ("claude.exe" if IS_WINDOWS else "claude"),  # native installer
            Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",            # npm -g
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "claude.exe",
        ]
        for c in candidates:
            if str(c) and c.exists():
                return str(c)
        return shutil.which("claude")

    def _run(self, args: list[str], timeout: float, stdin: str | None = None) -> subprocess.CompletedProcess:
        cmd = [self.exe] + args
        if IS_WINDOWS and self.exe.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd.exe", "/c"] + cmd
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)  # allow running even if Prism was launched from inside Claude Code
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0  # type: ignore[attr-defined]
        return subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(HERE), env=env, creationflags=flags,
        )

    def check(self) -> dict:
        """Cheap health check: is Claude Code installed and signed in? (no tokens used)"""
        self.exe = self.exe if (self.exe and Path(self.exe).exists()) else self._find_claude()
        if not self.exe:
            st = {"ok": False, "state": "missing", "detail": "Claude Code not installed — click Connect brain"}
        else:
            try:
                r = self._run(["auth", "status", "--json"], timeout=20)
                info = json.loads(r.stdout or "{}")
                if info.get("loggedIn"):
                    who = info.get("email") or info.get("account", {}).get("email") or info.get("authMethod", "")
                    st = {"ok": True, "state": "ready", "detail": f"Claude Code · {who}".strip(" ·")}
                else:
                    st = {"ok": False, "state": "signed_out", "detail": "Claude Code installed · not signed in — click Connect brain"}
            except Exception as e:
                st = {"ok": True, "state": "unknown", "detail": f"Claude Code found ({str(e)[:60]})"}
        if st["state"] != self.status.get("state"):
            log(f"[{'green' if st['ok'] else 'yellow'}]brain:[/] {st['detail']}")
        self.status = st
        return st

    def _health_loop(self):
        while True:
            try:
                self.check()
            except Exception as e:
                self.status = {"ok": False, "state": "error", "detail": str(e)[:120]}
            # re-check often until ready (e.g. while you're signing in), then rarely
            time.sleep(8 if not self.status.get("ok") else 300)

    def _system(self) -> str:
        now = datetime.now().strftime("%A %d %B %Y, %I:%M %p").lstrip("0")
        hist = "\n".join(f"User: {u}\nPrism: {a}" for u, a in self.history) or "(none yet)"
        return f"""You are PRISM, a calm, understated British voice assistant running on {self.address}'s Windows PC.
Everything you write will be spoken aloud by a text-to-speech voice, so:
- Reply in British English, warm but brief: one or two short sentences. No lists, no markdown, no emojis, no URLs.
- You may use WebSearch/WebFetch when the answer depends on current information (news, prices, scores,
  opening hours, anything recent). Keep it quick: one or two searches, then answer. Otherwise answer directly.
- Never try to edit files or run commands.
- If asked to open, launch, show, or pull up one of the registered apps, include its key in "open".
- If the user is clearly ending the conversation (goodbye, that's all, go to sleep, thanks that's it), set "sleep": true.

Respond with ONLY a single JSON object, nothing else:
{{"say": "<spoken reply>", "open": ["<app key>", ...], "sleep": false}}

Registered apps (key -> name):
{self.launcher.catalogue()}

Context:
- Local time: {now}
- Weather: {self.weather.short()}

Recent conversation:
{hist}
"""

    def ask(self, text: str) -> dict:
        if not self.exe or self.status.get("state") in ("missing", "signed_out"):
            st = self.check()
            if st["state"] == "missing":
                return {"say": "My brain isn't installed yet. Press Connect brain on the HUD, or run setup brain, and I'll be fully operational.", "open": [], "sleep": False}
            if st["state"] == "signed_out":
                return {"say": "Claude Code is installed but not signed in. Press Connect brain on the HUD to sign in.", "open": [], "sleep": False}

        tools = [t for t in self.cfg.get("tools", ["WebSearch", "WebFetch"]) if t]
        blocked = [t for t in self.ALWAYS_BLOCKED if t not in tools]
        args = ["-p", "Respond to the spoken request in the piped input, as PRISM, with the JSON object described.",
                "--output-format", "json", "--max-turns", str(int(self.cfg.get("max_turns", 6)))]
        if tools:
            args += ["--allowedTools", ",".join(tools)]
        if blocked:
            args += ["--disallowedTools", ",".join(blocked)]
        if self.cfg.get("model"):
            args += ["--model", self.cfg["model"]]

        prompt = self._system() + f"\nUser (spoken): {text}\n"
        try:
            t0 = time.monotonic()
            with self._lock:
                proc = self._run(args, timeout=float(self.cfg.get("timeout_s", 90)), stdin=prompt)
            log(f"[dim]brain responded in {time.monotonic() - t0:.1f}s[/dim]")
        except subprocess.TimeoutExpired:
            return {"say": "I'm afraid that took too long. Shall we try again?", "open": [], "sleep": False}
        except Exception as e:
            return {"say": f"I couldn't reach my brain: {e}", "open": [], "sleep": False}

        raw = proc.stdout.strip()
        if proc.returncode != 0 and not raw:
            err = (proc.stderr or "").strip().splitlines()
            tail = err[-1] if err else "unknown error"
            log(f"[red]claude exited {proc.returncode}:[/red] {tail}")
            if "login" in tail.lower() or "auth" in tail.lower() or "api key" in tail.lower():
                return {"say": "My brain isn't signed in. Press Connect brain on the HUD to sign in.", "open": [], "sleep": False}
            return {"say": "Something went wrong talking to Claude. Check the console for details.", "open": [], "sleep": False}

        result_text = raw
        try:
            outer = json.loads(raw)
            if isinstance(outer, dict):
                result_text = str(outer.get("result", raw))
                if outer.get("subtype") == "error_max_turns" and not result_text.strip():
                    return {"say": "That needed more digging than I can do in one go. Could you narrow it down?", "open": [], "sleep": False}
                if outer.get("is_error"):
                    log(f"[red]claude error:[/red] {result_text}")
                    low = result_text.lower()
                    if "auth" in low or "login" in low or "api key" in low:
                        say = "My brain isn't signed in. Press Connect brain on the HUD to sign in."
                    elif "rate" in low or "limit" in low or "usage" in low:
                        say = "I've hit my usage limit for the moment. Let's try again a little later."
                    else:
                        say = "Claude returned an error. The details are in the console."
                    return {"say": say, "open": [], "sleep": False}
        except json.JSONDecodeError:
            pass

        reply = self._parse_reply(result_text)
        self.history.append((text, reply["say"]))
        return reply

    @staticmethod
    def _parse_reply(text: str) -> dict:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                j = json.loads(m.group(0))
                return {
                    "say": str(j.get("say", "")).strip() or "Done.",
                    "open": [str(k) for k in (j.get("open") or [])],
                    "sleep": bool(j.get("sleep", False)),
                }
            except json.JSONDecodeError:
                pass
        clean = re.sub(r"[*_`#>]+", "", text).strip()
        return {"say": clean[:400] or "I'm not sure how to answer that.", "open": [], "sleep": False}


# --------------------------------------------------------------------------- #
# The assistant
# --------------------------------------------------------------------------- #
class Commands:
    """Loads commands.json: the voice phrases Prism understands, shown on the HUD's Commands tab."""

    def __init__(self, path: Path = COMMANDS_PATH):
        self.path = path
        self.data: dict = {"categories": []}
        self.compiled: list[tuple[str, re.Pattern]] = []
        self.reload()

    def reload(self):
        data = _read_json(self.path)
        if not isinstance(data, dict):
            return
        self.data = data
        self.compiled = []
        for cat in data.get("categories", []):
            for cmd in cat.get("commands", []):
                for pat in cmd.get("patterns", []) or []:
                    try:
                        self.compiled.append((cmd["id"], re.compile(pat, re.I)))
                    except re.error as e:
                        log(f"[yellow]Bad pattern in commands.json ({cmd.get('id')}):[/yellow] {e}")

    def match(self, text: str) -> str | None:
        t = " ".join(text.strip().split())
        for cid, rx in self.compiled:
            if rx.search(t):
                return cid
        return None


SLEEP_RE = re.compile(r"\b(good ?bye|that'?s all|that is all|go to sleep|never ?mind|stand down|thank you,? that'?s it|dismissed)\b", re.I)
WEATHER_RE = re.compile(r"\b(weather|forecast|temperature|how (?:hot|cold|warm) is it|is it (?:raining|going to rain))\b", re.I)
TIME_RE = re.compile(r"\b(what time is it|what'?s the time|time is it|what'?s the date|what day is it)\b", re.I)
MIC_OFF_RE = re.compile(r"\b(mute|turn off|switch off|disable|stop)\b.{0,12}\b(mic|microphone|listening)\b", re.I)
LIST_RE = re.compile(r"\b(what (?:apps |pages )?can you open|list (?:my |the )?apps|which apps)\b", re.I)


class Prism:
    STATES = ("booting", "sleeping", "listening", "thinking", "speaking")

    def __init__(self, cfg: dict, use_mic: bool = True):
        self.cfg = cfg
        self.name = cfg.get("assistant_name", "Prism")
        self.address = cfg.get("address_user_as", "sir")
        self.voice = Voice(cfg["voice"])
        self.weather = Weather(cfg["weather"])
        self.launcher = Launcher(load_apps(cfg.get("sync")))
        self.brain = Brain(cfg["brain"], self.launcher, self.weather, self.address)
        self.commands = Commands()
        from store_tasks import StoreTasks
        self.store = StoreTasks(cfg.get("store", {}), self.launcher, HERE, log)
        self.transcriber = Transcriber(cfg["listen"])
        self.detector = ClapDetector(cfg["wake"])
        self.mic: Mic | None = None
        self.mic_error = ""
        if use_mic:
            try:
                self.mic = Mic(cfg["listen"].get("input_device"))
            except Exception as e:
                self.mic_error = str(e)[:160]
                log(f"[red]Microphone unavailable:[/red] {e}. Running keyboard/HUD-only.")
        self.last_interaction = 0.0
        self.wake_event = threading.Event()
        self.typed: "queue.Queue[str]" = queue.Queue()
        self.state = "booting"
        self.status_line = ""
        self.wake_count = 0
        self.started = time.time()
        self.ignore_mic_until = 0.0
        self.transcript: deque[dict] = deque(maxlen=60)
        self.hud_url = ""
        self.mic_on = not bool(cfg["listen"].get("start_muted", False))

    # ---- HUD plumbing -----------------------------------------------------
    def set_state(self, state: str, line: str = ""):
        self.state = state
        self.status_line = line

    def add_line(self, who: str, text: str):
        self.transcript.append({"who": who, "text": text, "t": time.time()})

    def submit(self, text: str):
        """Typed command from the HUD or console. Empty text = just wake."""
        text = (text or "").strip()
        if text:
            self.typed.put(text)
        self.wake_event.set()

    def snapshot(self) -> dict:
        w = self.weather
        return {
            "name": self.name,
            "address": self.address,
            "state": "muted" if (self.state == "sleeping" and not self.mic_on) else self.state,
            "mic_on": self.mic_on,
            "line": "Mic off — press Mic or M to listen again" if (self.state == "sleeping" and not self.mic_on) else self.status_line,
            "level": round(self.mic.level, 4) if self._mic_live() else 0.0,
            "threshold": self.detector.thresh,
            "noise": round(self.detector.noise, 5),
            "claps": self.detector.clap_count,
            "last_clap": self.detector.last_clap,
            "wakes": self.wake_count,
            "uptime": int(time.time() - self.started),
            "transcript": list(self.transcript),
            "weather": {"short": w.short(), "city": w.city, "summary": w.summary() if w.data else ""},
            "systems": {
                "brain": {"ok": bool(self.brain.status.get("ok")), "detail": self.brain.status.get("detail", ""),
                          "state": self.brain.status.get("state", "")},
                "mic": {"ok": self._mic_live(), "detail": ((self.mic.name if self.mic_on else "OFF · " + self.mic.name) if self.mic else (self.mic_error or "disabled"))},
                "voice": {
                    "ok": self.voice.last_engine != "none",
                    "detail": f'{self.cfg["voice"].get("edge_voice")} · {self.voice.last_engine or "ready"}'
                    + (f" · {self.voice.last_error}" if self.voice.last_error else ""),
                },
                "weather": {"ok": w.data is not None, "detail": w.city or "locating"},
                "wake": {"ok": self._mic_live(), "detail": f'double clap · threshold {self.detector.thresh}' if self.mic_on else "mic off · use HUD / Enter"},
                "stt": {"ok": True, "detail": self.cfg["listen"].get("stt_engine", "google")},
            },
            "apps": [
                {"key": a["key"], "name": a.get("name", a["key"]), "section": a.get("section", "other"), "url": a.get("url", "")}
                for a in self.launcher.apps
            ],
        }

    def set_mic(self, on: bool):
        self.mic_on = bool(on)
        if self.mic:
            self.mic.set_enabled(self.mic_on)
        self.detector.reset()
        log(f"[cyan]Microphone {'ON' if self.mic_on else 'OFF'}[/cyan]")

    def set_threshold(self, value: float):
        v = max(0.05, min(0.9, float(value)))
        self.detector.thresh = round(v, 2)
        self.cfg["wake"]["clap_threshold"] = self.detector.thresh
        # persist to your local config.json (git-ignored)
        try:
            data = _read_json(CONFIG_PATH) or {}
            if not isinstance(data, dict):
                data = {}
            data.setdefault("wake", {})["clap_threshold"] = self.detector.thresh
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log(f"[yellow]Couldn't save threshold:[/yellow] {e}")

    def _mic_live(self) -> bool:
        return self.mic is not None and self.mic_on

    def restart(self, speak: bool = True):
        """Relaunch via PRISM.bat (pulls the latest from GitHub), then exit this instance."""
        if speak:
            self._speak("Restarting to pick up the latest version. Back in a moment.")
        bat = HERE / "PRISM.bat"
        if IS_WINDOWS and bat.exists():
            # wait 3 s so this process releases the mic and the HUD port first
            subprocess.Popen(["cmd.exe", "/c", "start", "PRISM", "cmd.exe", "/c",
                              f'timeout /t 3 /nobreak >nul & "{bat}" --no-hud-open'], cwd=str(HERE))
        else:
            subprocess.Popen([sys.executable, str(HERE / "prism.py")], cwd=str(HERE))
        if self.mic:
            self.mic.stop()
        os._exit(0)

    def _cmd_show_hud(self):
        show_hud(self.hud_url)
        self._speak("Here I am.")

    def _cmd_restart(self):
        self.restart()

    def setup_brain(self):
        """Open setup_brain.bat in its own console: installs Claude Code if needed, then signs in."""
        bat = HERE / "setup_brain.bat"
        if IS_WINDOWS and bat.exists():
            subprocess.Popen(["cmd.exe", "/c", "start", "PRISM brain setup", "cmd.exe", "/c", str(bat)], cwd=str(HERE))
            self.add_line("prism", "Opening brain setup. Approve the sign-in in your browser when it appears.")
        else:
            self.add_line("prism", "Run: claude auth login")

    def test_voice(self):
        self._speak(f"All systems operational, {self.address}. Voice link confirmed.")

    # ---- helpers ----------------------------------------------------------
    def greeting(self) -> str:
        h = datetime.now().hour
        part = "Good morning" if h < 12 else "Good afternoon" if h < 18 else "Good evening"
        since = (time.time() - self.last_interaction) / 60 if self.last_interaction else 1e9
        if since >= float(self.cfg.get("regreet_after_minutes", 60)):
            return f"{part}, {self.address}. {self.weather.summary()} How can I help?"
        return f"Yes, {self.address}?"

    def _keyboard_thread(self):
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt, RuntimeError):
                return
            self.submit(line)

    def _speak(self, text: str):
        prev = self.state
        self.set_state("speaking", text)
        self.add_line("prism", text)
        self.ignore_mic_until = float("inf")  # never hear our own voice as a clap
        try:
            self.voice.say(text)
        finally:
            self.ignore_mic_until = time.monotonic() + 0.6
            if self.mic:
                self.mic.flush()
            self.detector.reset()
            if self.state == "speaking":
                self.set_state(prev if prev != "speaking" else "sleeping")

    # ---- store tasks (ids from commands.json) -------------------------------
    def _cmd_tasks_today(self):
        self.set_state("thinking", "Checking your store tasks…")
        self._speak(self.store.today_say())

    def _cmd_ms_count(self):
        self.set_state("thinking", "Pulling the last 7 days of missed sales…")
        entries, start, end = self.store.ms_entries()
        self._speak(self.store.ms_say(self.store.ms_stats(entries, start, end)))

    def _cmd_ms_report(self):
        self.set_state("thinking", "Building the missed sales report…")
        path, stats = self.store.ms_report()
        self.add_line("prism", f"Report saved: {path}")
        self._speak(self.store.ms_say(stats, report=True))

    def _cmd_cost_count(self):
        self.set_state("thinking", "Checking ConsignR for missing costs…")
        self._speak(self.store.cost_say(*self.store.cost_missing()))

    def _cmd_cost_open(self):
        self.store.open_cost_fixer()
        self.set_state("thinking", "Opening Cost Fixer · counting missing costs…")
        try:
            msg = self.store.cost_say(*self.store.cost_missing())
        except Exception as e:
            msg = f"I couldn't get the count: {e}."
        self._speak(f"Opening Cost Fixer. {msg}")

    def _cmd_liquidation(self):
        opened = self.store.open_liquidation()
        self.add_line("prism", "Opened: " + ", ".join(opened))
        self._speak("Liquidation tool's up, with your StockX and eBay sales alongside. Add anything that's sold.")

    def _cmd_run_all(self):
        self.set_state("thinking", "Running your store tasks · missed sales…")
        try:
            path, stats = self.store.ms_report()
            self.add_line("prism", f"Report saved: {path}")
            self._speak("First, missed sales. " + self.store.ms_say(stats, report=True))
        except Exception as e:
            self._speak(f"I couldn't build the missed sales report: {e}.")
        self.store.open_cost_fixer()
        self.set_state("thinking", "Running your store tasks · cost fixer…")
        try:
            self._speak("Next, Cost Fixer. " + self.store.cost_say(*self.store.cost_missing()))
        except Exception as e:
            self._speak(f"Cost Fixer is open, but I couldn't get the count: {e}.")
        self.store.open_liquidation()
        self._speak("And liquidation is up, with StockX and eBay alongside. That's everything.")

    # ---- command handling -------------------------------------------------
    def handle(self, text: str) -> bool:
        """Returns True if the assistant should go back to sleep."""
        log(f"[bold]You:[/bold] {text}")
        self.add_line("you", text)
        self.last_interaction = time.time()

        if MIC_OFF_RE.search(text):
            self._speak("Microphone off. Use the HUD to switch it back on.")
            self.set_mic(False)
            return True

        if SLEEP_RE.search(text):
            self._speak("Very good. Standing by.")
            return True

        cid = self.commands.match(text)
        if cid and hasattr(self, f"_cmd_{cid}"):
            try:
                getattr(self, f"_cmd_{cid}")()
            except Exception as e:  # StoreError and anything unexpected: say it, don't crash
                log(f"[red]{cid} failed:[/red] {e}")
                self._speak(f"I couldn't finish that, {self.address}: {e}.")
            return False

        if LIST_RE.search(text):
            vercel = [a.get("name", a["key"]) for a in self.launcher.apps if a.get("section") == "vercel"]
            arts = [a.get("name", a["key"]) for a in self.launcher.apps if a.get("section") != "vercel"]
            msg = ""
            if vercel:
                msg += "Your Vercel projects: " + ", ".join(vercel) + ". "
            msg += f"Plus {len(arts)} Claude artifacts, such as " + ", ".join(arts[:6]) + "."
            self._speak(msg)
            return False

        if TIME_RE.search(text) and len(text.split()) <= 7:
            now = datetime.now()
            self._speak(f"It's {now.strftime('%I:%M %p').lstrip('0')} on {now.strftime('%A, %d %B')}.")
            return False

        if WEATHER_RE.search(text) and len(text.split()) <= 9:
            self._speak(self.weather.summary())
            return False

        m = OPEN_RE.match(text)
        if m:
            app, score = self.launcher.match(m.group(1))
            if app and score >= 0.6:
                self.launcher.open(app)
                self._speak(f"Opening {app.get('name', app['key'])}.")
                self.brain.history.append((text, f"Opening {app.get('name')}."))
                return False

        self.set_state("thinking", text)
        reply = self.brain.ask(text)
        self.set_state("thinking", "")
        for key in reply.get("open", []):
            app = self.launcher.by_key(key)
            if app:
                self.launcher.open(app)
        self._speak(reply["say"])
        return bool(reply.get("sleep"))

    # ---- main loop --------------------------------------------------------
    def run(self):
        log(f"[bold magenta]{self.name} online.[/bold magenta]  Clap twice to wake — or press Enter / use the HUD.")
        if self.hud_url:
            log(f"[cyan]HUD:[/cyan] {self.hud_url}")
        if not self.brain.exe:
            log("[yellow]Claude Code not installed yet[/yellow] — press Connect brain on the HUD (or run setup_brain.bat).")
        threading.Thread(target=self._keyboard_thread, daemon=True).start()
        if self.mic:
            self.mic.start()
            if not self.mic_on:
                self.mic.set_enabled(False)
            log(f"[dim]mic: {self.mic.name} @ {SAMPLE_RATE} Hz, clap threshold {self.detector.thresh}[/dim]")
        self._speak(f"{self.name} online.")

        try:
            while True:
                self._sleep_until_wake()
                self._conversation()
        except KeyboardInterrupt:
            pass
        finally:
            if self.mic:
                self.mic.stop()
            log("[dim]PRISM stopped.[/dim]")

    def _sleep_until_wake(self):
        self.set_state("sleeping", "Clap twice to wake" if self.mic_on else "Mic off — wake from the HUD")
        log("[dim]…sleeping (clap twice)[/dim]")
        self.wake_event.clear()
        self.detector.reset()
        cooldown_until = time.monotonic() + float(self.cfg["wake"].get("cooldown_s", 2.0))
        while True:
            if self.wake_event.is_set():
                self.wake_event.clear()
                self.wake_count += 1
                return
            if not self._mic_live():
                time.sleep(0.1)
                continue
            b = self.mic.read(0.25)
            if b is None:
                continue
            now = time.monotonic()
            if now < cooldown_until or now < self.ignore_mic_until:
                continue
            if self.detector.feed(b):
                log("[bold green]👏👏  double clap[/bold green]")
                self.wake_count += 1
                return

    def _take_typed(self) -> str | None:
        try:
            return self.typed.get_nowait()
        except queue.Empty:
            return None

    def _conversation(self):
        if self.mic:
            self.mic.flush()
        typed = self._take_typed()
        if not typed:
            self._speak(self.greeting())
        while True:
            text = typed or self._take_typed()
            typed = None
            if not text:
                if not self._mic_live():
                    text = self._await_typed(float(self.cfg["listen"].get("followup_window_s", 7)) * 3)
                    if text is None:
                        self.set_state("sleeping")
                        return
                else:
                    text = self._listen_once()
                    if text is None:
                        return
            if self.handle(text):
                return
            if self._mic_live() and self._await_followup():
                return
            if not self._mic_live():
                nxt = self._await_typed(20)
                if nxt is None:
                    return
                typed = nxt

    def _await_typed(self, timeout: float) -> str | None:
        self.set_state("listening", "Mic off — type a command")
        try:
            return self.typed.get(timeout=timeout)
        except queue.Empty:
            return None

    def _listen(self, cfg: dict) -> str | None:
        """Listen for one phrase; a typed HUD command interrupts and wins."""
        self.set_state("listening", "Listening…")
        self.mic.flush()
        audio = record_utterance(self.mic, cfg, self.detector.noise, should_abort=lambda: not self.typed.empty() or not self.mic_on)
        typed = self._take_typed()
        if typed:
            return typed
        if audio is None:
            return None
        self.set_state("thinking", "Transcribing…")
        text = self.transcriber.transcribe(audio)
        if not text:
            return ""
        return text

    def _listen_once(self) -> str | None:
        log("[cyan]listening…[/cyan]")
        for attempt in range(2):
            text = self._listen(self.cfg["listen"])
            if text is None:
                self._speak("Standing by.")
                return None
            if text:
                return text
            if attempt == 0:
                self._speak("Sorry, I didn't catch that.")
        self._speak("Standing by.")
        return None

    def _await_followup(self) -> bool:
        """After answering, keep listening briefly. Returns True when it's time to sleep."""
        window = float(self.cfg["listen"].get("followup_window_s", 7))
        if window <= 0:
            return True
        cfg = dict(self.cfg["listen"])
        cfg["no_speech_timeout_s"] = window
        log(f"[dim]follow-up window ({int(window)} s)…[/dim]")
        while True:
            text = self._listen(cfg)
            if text is None:
                return True  # silence -> sleep quietly
            if not text:
                continue
            if self.handle(text):
                return True


# --------------------------------------------------------------------------- #
# HUD — local JARVIS-style interface (http://127.0.0.1:7777)
# --------------------------------------------------------------------------- #
HUD_DIR = HERE / "hud"


def start_hud(prism: Prism, cfg: dict) -> str:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console clean
            pass

        def _send(self, code: int, body, ctype: str = "application/json"):
            data = body if isinstance(body, bytes) else str(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, (HUD_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/state":
                return self._send(200, json.dumps(prism.snapshot()))
            if path == "/commands":
                prism.commands.reload()
                return self._send(200, json.dumps({
                    "categories": prism.commands.data.get("categories", []),
                    "apps": [{"key": a["key"], "name": a.get("name", a["key"]), "section": a.get("section", "other"),
                              "aliases": a.get("aliases", [])[:3]} for a in prism.launcher.apps],
                }))
            return self._send(404, '{"error":"not found"}')

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except json.JSONDecodeError:
                data = {}
            path = self.path.split("?")[0]
            if path == "/cmd":
                prism.submit(str(data.get("text", "")))
            elif path == "/wake":
                prism.submit("")
            elif path == "/mic":
                prism.set_mic(bool(data.get("on", not prism.mic_on)))
            elif path == "/threshold":
                prism.set_threshold(float(data.get("value", prism.detector.thresh)))
            elif path == "/show":
                show_hud(prism.hud_url)
            elif path == "/restart":
                threading.Thread(target=prism.restart, daemon=True).start()
            elif path == "/setup-brain":
                prism.setup_brain()
            elif path == "/test-voice":
                threading.Thread(target=prism.test_voice, daemon=True).start()
            elif path == "/open":
                app = prism.launcher.by_key(str(data.get("key", "")))
                if app:
                    prism.launcher.open(app)
            else:
                return self._send(404, '{"error":"not found"}')
            return self._send(200, '{"ok":true}')

    port = int(cfg.get("port", 7777))
    for p in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("no free port for the HUD")
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/"


HUD_TITLE = "PRISM"


def _hud_browser() -> str | None:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    for exe in (
        rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
        rf"{pf}\Microsoft\Edge\Application\msedge.exe",
        rf"{pf}\Google\Chrome\Application\chrome.exe",
        rf"{local}\Google\Chrome\Application\chrome.exe",
    ):
        if exe and os.path.exists(exe):
            return exe
    return None


def open_hud_window(url: str, app_window: bool = True) -> None:
    """Open the HUD as its own app window.

    It uses a dedicated browser profile so Windows gives it its own taskbar button
    (otherwise it's grouped under your normal Edge icon and 'vanishes' when minimised).
    """
    exe = _hud_browser() if (IS_WINDOWS and app_window) else None
    if exe:
        profile = Path(os.environ.get("LOCALAPPDATA", str(HERE))) / "PRISM" / "hud-profile"
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1440,900",
                          "--no-first-run", "--no-default-browser-check", "--disable-features=msEdgeSidebarV2"])
        return
    webbrowser.open(url)


def _find_hud_hwnds() -> list[int]:
    """Top-level browser windows titled PRISM (the HUD), visible or minimised."""
    if not IS_WINDOWS:
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if buf.value.strip() == HUD_TITLE and cls.value.startswith("Chrome_WidgetWin"):
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


def show_hud(url: str) -> None:
    """Bring the HUD back: restore + focus it if it exists, otherwise open a new one."""
    hwnds = _find_hud_hwnds()
    if not hwnds:
        open_hud_window(url)
        return
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    for h in hwnds[:1]:
        user32.ShowWindow(h, 9)  # SW_RESTORE
        user32.SetForegroundWindow(h)


class DesktopShell:
    """Ways back to the HUD: a tray icon (click = show HUD) and a global hotkey (Ctrl+Alt+P)."""

    def __init__(self, prism: "Prism", cfg: dict):
        self.prism = prism
        self.cfg = cfg
        self.icon = None

    def start(self):
        if not IS_WINDOWS:
            return
        if self.cfg.get("hotkey", True):
            threading.Thread(target=self._hotkey_loop, daemon=True).start()
        if self.cfg.get("tray", True):
            threading.Thread(target=self._tray, daemon=True).start()

    def show(self):
        if self.prism.hud_url:
            show_hud(self.prism.hud_url)

    # -- Ctrl+Alt+P -------------------------------------------------------
    def _hotkey_loop(self):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x0312
        if not user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("P")):
            log("[yellow]Ctrl+Alt+P is taken by another app — use the tray icon instead.[/yellow]")
            return
        log("[dim]hotkey: Ctrl+Alt+P shows the HUD[/dim]")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                self.show()

    # -- tray icon ----------------------------------------------------------
    @staticmethod
    def _icon_image():
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((3, 3, 61, 61), outline=(55, 228, 255, 255), width=6)
        d.ellipse((22, 22, 42, 42), fill=(55, 228, 255, 255))
        return img

    def _tray(self):
        try:
            import pystray
        except Exception:
            log("[yellow]Tray icon unavailable (pystray not installed) — Ctrl+Alt+P still works.[/yellow]")
            return
        p = self.prism
        menu = pystray.Menu(
            pystray.MenuItem("Show HUD   (Ctrl+Alt+P)", lambda *_: self.show(), default=True),
            pystray.MenuItem("Microphone on", lambda *_: p.set_mic(not p.mic_on), checked=lambda _: p.mic_on),
            pystray.MenuItem("Test voice", lambda *_: threading.Thread(target=p.test_voice, daemon=True).start()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart Prism (update)", lambda *_: threading.Thread(target=p.restart, kwargs={"speak": False}, daemon=True).start()),
            pystray.MenuItem("Quit Prism", lambda *_: self._quit()),
        )
        self.icon = pystray.Icon("PRISM", self._icon_image(), "PRISM — click to show the HUD", menu)
        self.icon.run()

    def _quit(self):
        log("[dim]Quit from tray.[/dim]")
        try:
            if self.icon:
                self.icon.stop()
        finally:
            if self.prism.mic:
                self.prism.mic.stop()
            os._exit(0)


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #
def list_devices():
    import sounddevice as sd

    print(sd.query_devices())
    print("\nDefault input device:", sd.default.device[0])
    print("Set listen.input_device in config.json to an index above if the wrong mic is used.")


def calibrate(cfg: dict, seconds: int = 20):
    mic = Mic(cfg["listen"].get("input_device"))
    det = ClapDetector(cfg["wake"])
    mic.start()
    print(f"Calibration on '{mic.name}': speak normally, then clap twice. Current threshold = {det.thresh}")
    print("Peaks shown as a bar; claps should land well above the threshold line.\n")
    t_end = time.monotonic() + seconds
    max_quiet = 0.0
    try:
        while time.monotonic() < t_end:
            b = mic.read(0.5)
            if b is None:
                continue
            peak, _ = block_levels(b)
            fired = det.feed(b)
            bar = "#" * int(peak * 60)
            mark = "|" if int(det.thresh * 60) < len(bar) else " "
            line = f"{peak:5.2f} {bar:<60}{mark}"
            if fired:
                line += "   <<< DOUBLE CLAP"
            if peak < det.thresh:
                max_quiet = max(max_quiet, peak)
            print(line)
    finally:
        mic.stop()
    print(f"\nLoudest non-clap peak seen: {max_quiet:.2f}.")
    print(f"Suggested wake.clap_threshold: {min(0.9, max(0.12, round(max_quiet * 1.6, 2)))}")


def main():
    ap = argparse.ArgumentParser(description="PRISM voice assistant")
    ap.add_argument("--calibrate", action="store_true", help="live mic meter to tune the clap threshold")
    ap.add_argument("--devices", action="store_true", help="list audio input devices")
    ap.add_argument("--say", metavar="TEXT", help="test the voice and exit")
    ap.add_argument("--text", action="store_true", help="keyboard-only mode (no microphone)")
    ap.add_argument("--no-hud", action="store_true", help="don't start or open the HUD")
    ap.add_argument("--no-hud-open", action="store_true", help="start the HUD server but don't open a new window (used by restart)")
    args = ap.parse_args()

    cfg = load_config()
    if args.devices:
        return list_devices()
    if args.say:
        return Voice(cfg["voice"]).say(args.say)
    if args.calibrate:
        return calibrate(cfg)

    prism = Prism(cfg, use_mic=not args.text)
    hud_cfg = cfg.get("hud", {})
    if not args.no_hud and hud_cfg.get("enabled", True):
        try:
            prism.hud_url = start_hud(prism, hud_cfg)
            if hud_cfg.get("open_on_start", True) and not args.no_hud_open:
                open_hud_window(prism.hud_url, hud_cfg.get("app_window", True))
            DesktopShell(prism, hud_cfg).start()
        except Exception as e:
            log(f"[yellow]HUD unavailable:[/yellow] {e}")
    prism.run()


if __name__ == "__main__":
    main()
