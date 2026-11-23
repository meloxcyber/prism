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
        "timeout_s": 90,
        "history_turns": 8,
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
                with open(APPS_PATH, "w", encoding="utf-8") as f:  # cache for offline use
                    json.dump(remote, f, indent=2, ensure_ascii=False)
                log(f"[dim]synced {len(apps)} apps from {url}[/dim]")
        except Exception as e:
            log(f"[yellow]App sync failed ({e}); using local apps.json[/yellow]")
    if not apps:
        apps = _apps_from(_read_json(APPS_PATH))

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
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=BLOCK,
            device=device,
            callback=self._cb,
        )

    def _cb(self, indata, frames, t, status):  # noqa: D401
        self.q.put(indata[:, 0].copy())

    def start(self):
        self.stream.start()

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
def record_utterance(mic: Mic, cfg: dict, noise_rms: float) -> np.ndarray | None:
    speech_thresh = max(0.012, noise_rms * 4.0)
    frames: list[np.ndarray] = []
    started = False
    start_idx = 0
    t_start = time.monotonic()
    last_voice = t_start

    while True:
        b = mic.read(1.0)
        if b is None:
            continue
        now = time.monotonic()
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
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._mixer_ready = False

    def say(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        log(f"[bold cyan]PRISM:[/bold cyan] {text}")
        if self.cfg.get("engine", "edge") == "edge":
            try:
                self._edge(text)
                return
            except Exception as e:
                log(f"[yellow]Neural voice unavailable ({e}); using offline voice.[/yellow]")
        self._sapi(text)

    def _edge(self, text: str) -> None:
        import edge_tts
        import pygame

        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="prism_")
        os.close(fd)

        async def gen():
            await edge_tts.Communicate(
                text,
                self.cfg.get("edge_voice", "en-GB-RyanNeural"),
                rate=self.cfg.get("rate", "-6%"),
                pitch=self.cfg.get("pitch", "-2Hz"),
            ).save(path)

        asyncio.run(gen())
        if not self._mixer_ready:
            pygame.mixer.init()
            self._mixer_ready = True
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.05)
        pygame.mixer.music.unload()
        try:
            os.remove(path)
        except OSError:
            pass

    def _sapi(self, text: str) -> None:
        try:
            import pyttsx3
        except Exception:
            print(f"(voice unavailable) {text}")
            return
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
    DISALLOWED = "Bash,Edit,Write,MultiEdit,NotebookEdit,Task,WebFetch,WebSearch,Read,Glob,Grep,TodoWrite"

    def __init__(self, cfg: dict, launcher: Launcher, weather: Weather, address: str):
        self.cfg = cfg
        self.launcher = launcher
        self.weather = weather
        self.address = address
        self.history: deque[tuple[str, str]] = deque(maxlen=int(cfg.get("history_turns", 8)))
        self.exe = self._find_claude()

    def _find_claude(self) -> str | None:
        if self.cfg.get("claude_path"):
            return self.cfg["claude_path"]
        found = shutil.which("claude")
        if found:
            return found
        home = Path.home()
        candidates = [
            home / ".local" / "bin" / "claude.exe",
            home / ".local" / "bin" / "claude",
            Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
            home / "AppData" / "Roaming" / "npm" / "claude.cmd",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return None

    def _system(self) -> str:
        now = datetime.now().strftime("%A %d %B %Y, %I:%M %p").lstrip("0")
        hist = "\n".join(f"User: {u}\nPrism: {a}" for u, a in self.history) or "(none yet)"
        return f"""You are PRISM, a calm, understated British voice assistant running on {self.address}'s Windows PC.
Everything you write will be spoken aloud by a text-to-speech voice, so:
- Reply in British English, warm but brief: one or two short sentences. No lists, no markdown, no emojis, no URLs.
- Never use tools. Answer from what you know and the context below.
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
        if not self.exe:
            return {"say": "I can't find the Claude Code command on this machine. Install Claude Code, run claude once to sign in, and start me again.", "open": [], "sleep": False}

        prompt = self._system() + f"\nUser (spoken): {text}\n"
        cmd = [self.exe, "-p", "Respond to the spoken request in the piped input, as PRISM, with the JSON object described.",
               "--output-format", "json", "--max-turns", "1", "--disallowedTools", self.DISALLOWED]
        if self.cfg.get("model"):
            cmd += ["--model", self.cfg["model"]]
        if IS_WINDOWS and self.exe.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd.exe", "/c"] + cmd
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0  # type: ignore[attr-defined]

        env = dict(os.environ)
        env.pop("CLAUDECODE", None)  # allow running even if launched from inside Claude Code
        try:
            t0 = time.monotonic()
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=float(self.cfg.get("timeout_s", 90)), cwd=str(HERE), env=env, creationflags=flags,
            )
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
                return {"say": "My brain isn't signed in. Please open a terminal, run claude, and log in.", "open": [], "sleep": False}
            return {"say": "Something went wrong talking to Claude. Check the console for details.", "open": [], "sleep": False}

        result_text = raw
        try:
            outer = json.loads(raw)
            if isinstance(outer, dict):
                result_text = str(outer.get("result", raw))
                if outer.get("is_error"):
                    log(f"[red]claude error:[/red] {result_text}")
                    low = result_text.lower()
                    if "auth" in low or "login" in low or "api key" in low:
                        say = "My brain isn't signed in. Please open a terminal, run claude, and log in."
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
SLEEP_RE = re.compile(r"\b(good ?bye|that'?s all|that is all|go to sleep|never ?mind|stand down|thank you,? that'?s it|dismissed)\b", re.I)
WEATHER_RE = re.compile(r"\b(weather|forecast|temperature|how (?:hot|cold|warm) is it|is it (?:raining|going to rain))\b", re.I)
TIME_RE = re.compile(r"\b(what time is it|what'?s the time|time is it|what'?s the date|what day is it)\b", re.I)
LIST_RE = re.compile(r"\b(what (?:apps |pages )?can you open|list (?:my |the )?apps|which apps)\b", re.I)


class Prism:
    def __init__(self, cfg: dict, use_mic: bool = True):
        self.cfg = cfg
        self.name = cfg.get("assistant_name", "Prism")
        self.address = cfg.get("address_user_as", "sir")
        self.voice = Voice(cfg["voice"])
        self.weather = Weather(cfg["weather"])
        self.launcher = Launcher(load_apps(cfg.get("sync")))
        self.brain = Brain(cfg["brain"], self.launcher, self.weather, self.address)
        self.transcriber = Transcriber(cfg["listen"])
        self.detector = ClapDetector(cfg["wake"])
        self.mic = Mic(cfg["listen"].get("input_device")) if use_mic else None
        self.last_interaction = 0.0
        self.wake_event = threading.Event()
        self.typed: "queue.Queue[str]" = queue.Queue()
        self.last_wake_at = 0.0

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
            except (EOFError, KeyboardInterrupt):
                return
            line = line.strip()
            if line:
                self.typed.put(line)
            self.wake_event.set()

    def _speak(self, text: str):
        self.voice.say(text)
        if self.mic:
            self.mic.flush()
        self.detector.reset()

    # ---- command handling -------------------------------------------------
    def handle(self, text: str) -> bool:
        """Returns True if the assistant should go back to sleep."""
        log(f"[bold]You:[/bold] {text}")
        self.last_interaction = time.time()

        if SLEEP_RE.search(text):
            self._speak("Very good. Standing by.")
            return True

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
                self._speak(f"Opening {app.get('name', app['key'])}.")
                self.launcher.open(app)
                self.brain.history.append((text, f"Opening {app.get('name')}."))
                return False

        reply = self.brain.ask(text)
        opened = []
        for key in reply.get("open", []):
            app = self.launcher.by_key(key)
            if app:
                self.launcher.open(app)
                opened.append(app.get("name", key))
        self._speak(reply["say"])
        return bool(reply.get("sleep"))

    # ---- main loop --------------------------------------------------------
    def run(self):
        log(f"[bold magenta]{self.name} online.[/bold magenta]  Clap twice to wake — or press Enter. Type a command + Enter to test without the mic.")
        if self.brain.exe:
            log(f"[dim]brain: {self.brain.exe}[/dim]")
        else:
            log("[red]Claude Code CLI not found.[/red] Set brain.claude_path in config.json or install Claude Code.")
        threading.Thread(target=self._keyboard_thread, daemon=True).start()
        if self.mic:
            self.mic.start()
            log(f"[dim]mic open @ {SAMPLE_RATE} Hz, clap threshold {self.detector.thresh}[/dim]")
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
        log("[dim]…sleeping (clap twice)[/dim]")
        self.wake_event.clear()
        self.detector.reset()
        cooldown_until = time.monotonic() + float(self.cfg["wake"].get("cooldown_s", 2.0))
        while True:
            if self.wake_event.is_set():
                self.wake_event.clear()
                return
            if not self.mic:
                time.sleep(0.1)
                continue
            b = self.mic.read(0.5)
            if b is None:
                continue
            if time.monotonic() < cooldown_until:
                continue
            if self.detector.feed(b):
                log("[bold green]👏👏  double clap[/bold green]")
                return

    def _conversation(self):
        if self.mic:
            self.mic.flush()
        self.last_wake_at = time.time()
        first = True
        while True:
            typed = None
            try:
                typed = self.typed.get_nowait()
            except queue.Empty:
                pass

            if first:
                if not typed:
                    self._speak(self.greeting())
                first = False

            if typed:
                text = typed
            else:
                if not self.mic:
                    # text-only mode: wait for the next typed line
                    text = self.typed.get()
                else:
                    text = self._listen_once()
                    if text is None:
                        return

            if self.handle(text):
                return

            # follow-up window: keep listening briefly; silence -> back to sleep
            if self.mic:
                if self._await_followup():
                    return

    def _listen_once(self) -> str | None:
        log("[cyan]listening…[/cyan]")
        self.mic.flush()
        audio = record_utterance(self.mic, self.cfg["listen"], self.detector.noise)
        if audio is None:
            self._speak("Standing by.")
            return None
        text = self.transcriber.transcribe(audio)
        if not text:
            self._speak("Sorry, I didn't catch that.")
            return self._listen_once_retry()
        return text

    def _listen_once_retry(self) -> str | None:
        self.mic.flush()
        audio = record_utterance(self.mic, self.cfg["listen"], self.detector.noise)
        if audio is None:
            self._speak("Standing by.")
            return None
        return self.transcriber.transcribe(audio) or None

    def _await_followup(self) -> bool:
        """After answering, keep listening briefly. Returns True when it's time to sleep."""
        window = float(self.cfg["listen"].get("followup_window_s", 7))
        if window <= 0:
            return True
        cfg = dict(self.cfg["listen"])
        cfg["no_speech_timeout_s"] = window
        log(f"[dim]follow-up window ({int(window)} s)…[/dim]")
        self.mic.flush()
        while True:
            try:
                typed = self.typed.get_nowait()
                return self.handle(typed)
            except queue.Empty:
                pass
            audio = record_utterance(self.mic, cfg, self.detector.noise)
            if audio is None:
                return True  # silence -> sleep quietly
            text = self.transcriber.transcribe(audio)
            if not text:
                continue
            if self.handle(text):
                return True
            self.mic.flush()


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
    print(f"Calibration: speak normally, then clap twice. Current threshold = {det.thresh}")
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
    args = ap.parse_args()

    cfg = load_config()
    if args.devices:
        return list_devices()
    if args.say:
        return Voice(cfg["voice"]).say(args.say)
    if args.calibrate:
        return calibrate(cfg)

    Prism(cfg, use_mic=not args.text).run()


if __name__ == "__main__":
    main()
