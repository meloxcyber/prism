"""
Handful of Hype store tasks for PRISM.

  • Missed Sales  — pull the last N days from the Missed Sales app, speak a summary,
                    and build a PDF report (opened on screen).
  • Cost Fixer    — count store-owned ConsignR items with no cost (same rule as the app).
  • Liquidation   — open the Liquidation artifact + StockX Pro sales + eBay Seller Hub orders.

Credentials live only in your local config.json (git-ignored), under "store".
"""
from __future__ import annotations

import html
import json
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

IS_WINDOWS = os.name == "nt"

DEFAULTS = {
    "missed_sales_url": "https://hoh-missed-sales-amber.vercel.app",
    "missed_sales_key": "",
    "cost_fixer_url": "https://hoh-cost-fixer.vercel.app",
    "cost_fixer_password": "",
    "report_days": 7,
    "report_day": "Monday",
    "reports_dir": "",  # default: <JXRS>\Reports
    "liquidation_app_key": "liquidation",
    "liquidation_links": [
        {"name": "StockX sales", "url": "https://pro.stockx.com/history/completed"},
        {"name": "eBay orders", "url": "https://www.ebay.com/sh/ord/?filter=status:ALL_ORDERS"},
    ],
}

NUM_WORDS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
             "eleven", "twelve"]


def spoken(n: int) -> str:
    return NUM_WORDS[n] if 0 <= n < len(NUM_WORDS) else f"{n:,}"


def times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{spoken(n)} times")


def speakable(name: str) -> str:
    """'Jordan 11 Retro Space Jam (2026)' -> 'Jordan 11 Retro Space Jam'"""
    import re
    return re.sub(r"\s*\([^)]*\)", "", name or "").strip()


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{spoken(n)} {word if n == 1 else (many or word + 's')}"


class StoreError(Exception):
    """A problem worth saying out loud (missing password, service down…)."""


class StoreTasks:
    def __init__(self, cfg: dict, launcher, here: Path, log=print):
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.launcher = launcher
        self.here = here
        self.log = log
        self._cost_cache: tuple[float, tuple[int, int]] | None = None

    # ------------------------------------------------------------------ http
    def _get_json(self, url: str, headers: dict, timeout: float = 20):
        req = urllib.request.Request(url, headers={"User-Agent": "PRISM/1.0", "Accept": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise StoreError("the password was rejected") from e
            raise StoreError(f"the service answered {e.code}") from e
        except urllib.error.URLError as e:
            raise StoreError("I couldn't reach it — check the internet connection") from e

    # ----------------------------------------------------------- missed sales
    def ms_range(self, days: int | None = None) -> tuple[date, date]:
        days = int(days or self.cfg["report_days"])
        today = date.today()
        return today - timedelta(days=days - 1), today

    def ms_entries(self, days: int | None = None) -> tuple[list[dict], date, date]:
        key = str(self.cfg.get("missed_sales_key") or "").strip()
        if not key:
            raise StoreError("the Missed Sales key isn't set in config.json")
        start, end = self.ms_range(days)
        url = f'{self.cfg["missed_sales_url"].rstrip("/")}/api/history?' + urllib.parse.urlencode(
            {"from": start.isoformat(), "to": end.isoformat()})
        data = self._get_json(url, {"x-hoh-key": key})
        entries = data.get("entries", []) if isinstance(data, dict) else (data or [])
        entries = [e for e in entries if isinstance(e, dict)]
        entries.sort(key=lambda e: e.get("ts") or 0, reverse=True)
        return entries, start, end

    @staticmethod
    def offer_bucket(e: dict) -> str:
        o = str(e.get("offer") or "").lower()
        if o in ("accepted", "accept", "yes_accepted"):
            return "accepted"
        if o in ("passed", "declined", "pass"):
            return "passed"
        if o in ("no", "none", "not_offered", "didnt", "didnt_offer", "not offered", "notoffered"):
            return "not_offered"
        return "no_answer"

    def ms_stats(self, entries: list[dict], start: date, end: date) -> dict:
        per_day = Counter(e.get("day") or "" for e in entries)
        days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
        items: dict[str, dict] = {}
        for e in entries:
            name = (e.get("item") or "Unknown item").strip()
            it = items.setdefault(name, {"item": name, "sku": e.get("sku") or "", "count": 0, "sizes": Counter(), "last": ""})
            it["count"] += 1
            if e.get("size"):
                it["sizes"][str(e["size"])] += 1
            it["last"] = max(it["last"], e.get("day") or "")
            if not it["sku"] and e.get("sku"):
                it["sku"] = e["sku"]
        top = sorted(items.values(), key=lambda x: (-x["count"], x["item"]))
        sizes = Counter(str(e["size"]) for e in entries if e.get("size") and str(e["size"]).upper() != "OS")
        offers = Counter(self.offer_bucket(e) for e in entries)
        emp: dict[str, Counter] = defaultdict(Counter)
        for e in entries:
            name = (e.get("employee") or "No name").strip() or "No name"
            emp[name]["missed"] += 1
            emp[name][self.offer_bucket(e)] += 1
        offered = offers["accepted"] + offers["passed"]
        return {
            "start": start, "end": end, "days": days,
            "total": len(entries),
            "per_day": [(d, per_day.get(d, 0)) for d in days],
            "days_with_misses": sum(1 for d in days if per_day.get(d)),
            "distinct": len(items),
            "top": top,
            "sizes": sizes.most_common(8),
            "offers": offers,
            "offered": offered,
            "employees": sorted(emp.items(), key=lambda kv: -kv[1]["missed"]),
        }

    def ms_say(self, s: dict, *, report: bool = False) -> str:
        n = s["total"]
        span = f'the last {spoken(len(s["days"]))} days'
        if n == 0:
            msg = f"No missed sales in {span}. A clean week."
        else:
            msg = f"{plural(n, 'missed sale').capitalize()} in {span}"
            top = s["top"][0]
            if top["count"] > 1:
                msg += f". Top request was the {speakable(top['item'])}, {times(top['count'])}"
            elif n > 1:
                msg += f", across {plural(s['distinct'], 'different item')}"
            else:
                msg += f": the {speakable(top['item'])}"
            msg += "."
            if s["sizes"]:
                size, cnt = s["sizes"][0]
                if cnt > 1:
                    msg += f" Most asked size: {size}."
            if s["offered"]:
                acc = s["offers"]["accepted"]
                msg += f" Pre-orders offered on {spoken(s['offered'])} of them, {spoken(acc) if acc else 'none'} accepted."
        if report:
            msg += " The report's on screen."
        return msg

    # ------------------------------------------------------------ PDF report
    def reports_dir(self) -> Path:
        d = Path(self.cfg["reports_dir"]) if self.cfg.get("reports_dir") else self.here.parent / "Reports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def ms_report(self, days: int | None = None) -> tuple[Path, dict]:
        entries, start, end = self.ms_entries(days)
        s = self.ms_stats(entries, start, end)
        out = self.reports_dir() / f"Missed Sales Report {start.isoformat()} to {end.isoformat()}.pdf"
        page = self._report_html(entries, s)
        html_path = Path(tempfile.gettempdir()) / "prism_missed_sales_report.html"
        html_path.write_text(page, encoding="utf-8")
        pdf = self._print_pdf(html_path, out)
        target = pdf or html_path
        self._open_file(target)
        self.log(f"[green]Report:[/green] {target}")
        return target, s

    def _report_html(self, entries: list[dict], s: dict) -> str:
        esc = lambda x: html.escape(str(x if x is not None else ""))  # noqa: E731
        fmt = lambda d: d.strftime("%b %-d") if not IS_WINDOWS else d.strftime("%b %#d")  # noqa: E731
        peak = max([c for _, c in s["per_day"]] + [1])
        bars = "".join(
            f'<div class="bar"><div class="col"><span class="n">{c or ""}</span>'
            f'<div class="fill" style="height:{max(2, int(c / peak * 100)) if c else 2}%;{"" if c else "opacity:.25"}"></div></div>'
            f'<div class="d">{datetime.fromisoformat(d).strftime("%a")}<br>{datetime.fromisoformat(d).day}</div></div>'
            for d, c in s["per_day"]
        )
        top_rows = "".join(
            f"<tr><td>{esc(t['item'])}</td><td class='m'>{esc(t['sku']) or '—'}</td><td class='r b'>{t['count']}</td>"
            f"<td>{esc(', '.join(f'{k}×{v}' if v > 1 else k for k, v in t['sizes'].most_common()) or '—')}</td>"
            f"<td class='m'>{esc(t['last'])}</td></tr>"
            for t in s["top"][:12]
        ) or "<tr><td colspan=5 class='empty'>No missed sales in this period.</td></tr>"
        size_rows = "".join(f"<tr><td class='b'>{esc(k)}</td><td class='r'>{v}</td></tr>" for k, v in s["sizes"]) \
            or "<tr><td colspan=2 class='empty'>—</td></tr>"
        lbl = {"accepted": "Accepted", "passed": "Offered · passed", "not_offered": "Didn't offer", "no_answer": "No answer"}
        emp_rows = "".join(
            f"<tr><td class='b'>{esc(name)}</td><td class='r'>{c['missed']}</td><td class='r'>{c['accepted'] + c['passed']}</td>"
            f"<td class='r'>{c['accepted']}</td><td class='r'>{c['not_offered']}</td><td class='r'>{c['no_answer']}</td></tr>"
            for name, c in s["employees"]
        ) or "<tr><td colspan=6 class='empty'>—</td></tr>"
        log_rows = "".join(
            f"<tr><td class='m'>{esc(datetime.fromtimestamp((e.get('ts') or 0) / 1000).strftime('%a %b %d · %I:%M %p') if e.get('ts') else e.get('day'))}</td>"
            f"<td>{esc(e.get('item'))}</td><td class='m'>{esc(e.get('sku') or '')}</td><td class='b'>{esc(e.get('size') or '—')}</td>"
            f"<td>{esc(e.get('employee') or '—')}</td><td>{lbl[self.offer_bucket(e)]}</td><td>{esc(e.get('store') or '')}</td></tr>"
            for e in entries
        ) or "<tr><td colspan=7 class='empty'>Nothing logged.</td></tr>"
        offered_pct = f"{round(100 * s['offered'] / s['total'])}%" if s["total"] else "—"
        per_open_day = f"{s['total'] / max(1, len(s['days'])):.1f}"
        top_line = f"{esc(s['top'][0]['item'])} ({s['top'][0]['count']})" if s["top"] else "—"
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Missed Sales Report</title>
<style>
@page {{ size: Letter; margin: 14mm 13mm; }}
* {{ box-sizing: border-box; }}
body {{ font-family: "Segoe UI", system-ui, sans-serif; color: #10161c; margin: 0; font-size: 11pt; }}
.top {{ display:flex; justify-content:space-between; align-items:flex-end; border-bottom: 3px solid #10161c; padding-bottom: 10px; }}
.brand {{ font: 700 9pt "Segoe UI"; letter-spacing: .32em; color: #0a7ea4; }}
h1 {{ margin: 4px 0 0; font-size: 22pt; letter-spacing: -.01em; }}
.range {{ text-align:right; font-size: 10pt; color:#46525e; }}
.range b {{ display:block; font-size: 13pt; color:#10161c; }}
.kpis {{ display:grid; grid-template-columns: repeat(5, 1fr); gap: 8px; margin: 16px 0 6px; }}
.kpi {{ border:1px solid #d6dde3; border-top:3px solid #0a7ea4; padding: 9px 10px; }}
.kpi .v {{ font-size: 20pt; font-weight: 700; line-height: 1.1; }}
.kpi .l {{ font-size: 8pt; text-transform: uppercase; letter-spacing: .12em; color:#5b6773; margin-top: 3px; }}
.kpi .s {{ font-size: 8.5pt; color:#46525e; margin-top: 2px; }}
h2 {{ font-size: 9pt; text-transform: uppercase; letter-spacing: .22em; color:#0a7ea4; margin: 18px 0 8px; }}
.chart {{ display:flex; gap: 8px; height: 120px; align-items: stretch; border-bottom:1px solid #c5ced6; padding: 0 4px; }}
.bar {{ flex:1; display:flex; flex-direction:column; }}
.col {{ flex:1; display:flex; flex-direction:column; justify-content:flex-end; align-items:center; }}
.fill {{ width: 70%; background:#0a7ea4; border-radius: 2px 2px 0 0; }}
.n {{ font-size: 9pt; font-weight:700; margin-bottom: 2px; }}
.d {{ text-align:center; font-size: 8pt; color:#5b6773; padding-top: 4px; line-height:1.2; }}
.grid2 {{ display:grid; grid-template-columns: 2.2fr 1fr; gap: 14px; }}
table {{ width:100%; border-collapse: collapse; font-size: 9.5pt; }}
th {{ text-align:left; font-size: 7.5pt; text-transform: uppercase; letter-spacing:.12em; color:#5b6773; border-bottom: 1.5px solid #10161c; padding: 5px 6px; }}
td {{ padding: 5px 6px; border-bottom: 1px solid #e3e8ec; vertical-align: top; }}
tr:nth-child(even) td {{ background:#f6f8f9; }}
.r {{ text-align:right; }} .b {{ font-weight:700; }} .m {{ font-family: Consolas, monospace; font-size: 8.5pt; color:#46525e; }}
.empty {{ color:#7a8590; text-align:center; padding: 14px; }}
.foot {{ margin-top: 18px; font-size: 8pt; color:#7a8590; display:flex; justify-content:space-between; border-top:1px solid #d6dde3; padding-top: 6px; }}
.avoid {{ break-inside: avoid; }}
</style></head><body>
<div class="top">
  <div><div class="brand">HANDFUL OF HYPE</div><h1>Missed Sales Report</h1></div>
  <div class="range"><b>{fmt(s['start'])} – {fmt(s['end'])}, {s['end'].year}</b>Last {len(s['days'])} days · all stores</div>
</div>
<div class="kpis">
  <div class="kpi"><div class="v">{s['total']}</div><div class="l">Missed sales</div><div class="s">{per_open_day} per day</div></div>
  <div class="kpi"><div class="v">{s['days_with_misses']}/{len(s['days'])}</div><div class="l">Days with misses</div></div>
  <div class="kpi"><div class="v">{s['distinct']}</div><div class="l">Different items</div><div class="s">Top: {top_line}</div></div>
  <div class="kpi"><div class="v">{offered_pct}</div><div class="l">Offered pre-order</div><div class="s">{s['offered']} of {s['total']}</div></div>
  <div class="kpi"><div class="v">{s['offers']['accepted']}</div><div class="l">Pre-orders accepted</div><div class="s">{s['offers']['passed']} passed</div></div>
</div>
<div class="avoid"><h2>Missed sales per day</h2><div class="chart">{bars}</div></div>
<div class="grid2 avoid">
  <div><h2>Most requested items</h2><table><tr><th>Item</th><th>SKU</th><th class="r">Asks</th><th>Sizes</th><th>Last</th></tr>{top_rows}</table></div>
  <div><h2>Sizes asked for</h2><table><tr><th>Size</th><th class="r">Asks</th></tr>{size_rows}</table></div>
</div>
<div class="avoid"><h2>Pre-order offers by employee</h2><table><tr><th>Employee</th><th class="r">Missed</th><th class="r">Offered</th><th class="r">Accepted</th><th class="r">Didn't offer</th><th class="r">No answer</th></tr>{emp_rows}</table></div>
<h2>Full log</h2><table><tr><th>When</th><th>Item</th><th>SKU</th><th>Size</th><th>Employee</th><th>Pre-order</th><th>Store</th></tr>{log_rows}</table>
<div class="foot"><span>Source: Missed Sale Log · hoh-missed-sales-amber.vercel.app</span><span>Generated by PRISM · {datetime.now().strftime('%b %d, %Y %I:%M %p')}</span></div>
</body></html>"""

    @staticmethod
    def find_browser() -> str | None:
        if not IS_WINDOWS:
            for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
                p = __import__("shutil").which(name)
                if p:
                    return p
            return None
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        for exe in (rf"{pf86}\Microsoft\Edge\Application\msedge.exe", rf"{pf}\Microsoft\Edge\Application\msedge.exe",
                    rf"{pf}\Google\Chrome\Application\chrome.exe", rf"{local}\Google\Chrome\Application\chrome.exe"):
            if exe and os.path.exists(exe):
                return exe
        return None

    def _print_pdf(self, html_path: Path, pdf_path: Path) -> Path | None:
        exe = self.find_browser()
        if not exe:
            return None
        try:
            if pdf_path.exists():
                pdf_path.unlink()
        except OSError:
            pdf_path = pdf_path.with_name(pdf_path.stem + f" ({int(time.time())}).pdf")
        profile = Path(tempfile.gettempdir()) / "prism_pdf_profile"
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-pdf-header-footer",
               f"--user-data-dir={profile}", f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()]
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0  # type: ignore[attr-defined]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60, creationflags=flags)
        except Exception as e:
            self.log(f"[yellow]PDF print failed:[/yellow] {e}")
            return None
        for _ in range(20):  # the file can land a moment after the process exits
            if pdf_path.exists() and pdf_path.stat().st_size > 1000:
                return pdf_path
            time.sleep(0.25)
        return None

    @staticmethod
    def _open_file(path: Path):
        if IS_WINDOWS:
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            webbrowser.open(path.resolve().as_uri())

    # ------------------------------------------------------------ cost fixer
    def cost_missing(self, max_age_s: float = 120) -> tuple[int, int]:
        """(missing, total) for store-owned in-stock items — the Cost Fixer rule:
        owner.isStoreAccount is true and cost is blank ($0 counts as a cost)."""
        if self._cost_cache and time.time() - self._cost_cache[0] < max_age_s:
            return self._cost_cache[1]
        pw = str(self.cfg.get("cost_fixer_password") or "")
        if not pw:
            raise StoreError("the Cost Fixer password isn't set in config.json")
        base = self.cfg["cost_fixer_url"].rstrip("/")
        statuses = "status=ACTIVE&status=HOLD&status=EXPECTING&status=WITHDRAW_REQUESTED"
        missing = total = 0
        for page in range(1, 60):
            p = f"/items?{statuses}&limit=250&page={page}&sort=createdAt&order=asc"
            batch = self._get_json(f"{base}/api/consignr?p={urllib.parse.quote(p, safe='')}",
                                   {"x-app-password": pw}, timeout=30)
            if not isinstance(batch, list) or not batch:
                break
            for it in batch:
                owner = it.get("owner") or {}
                if not owner.get("isStoreAccount"):
                    continue
                total += 1
                c = it.get("cost")
                if c is None or str(c).strip() == "":
                    missing += 1
            if len(batch) < 250:
                break
        self._cost_cache = (time.time(), (missing, total))
        return missing, total

    def cost_say(self, missing: int, total: int) -> str:
        if missing == 0:
            return f"Every one of your {total:,} store-owned items has a cost. Nothing to fix."
        return f"{missing:,} of your {total:,} store-owned items {'is' if missing == 1 else 'are'} missing a cost."

    def open_cost_fixer(self):
        webbrowser.open(self.cfg["cost_fixer_url"])

    # ----------------------------------------------------------- liquidation
    def open_liquidation(self) -> list[str]:
        opened = []
        app = self.launcher.by_key(self.cfg.get("liquidation_app_key", "liquidation")) if self.launcher else None
        if app and app.get("url"):
            webbrowser.open(app["url"])
            opened.append(app.get("name", "Liquidation"))
        for link in self.cfg.get("liquidation_links", []):
            time.sleep(0.4)  # keep tab order predictable
            webbrowser.open_new_tab(link["url"])
            opened.append(link.get("name", link["url"]))
        return opened

    # ----------------------------------------------------------- task list
    def report_due_today(self) -> bool:
        return datetime.now().strftime("%A").lower() == str(self.cfg.get("report_day", "Monday")).lower()

    def today_say(self) -> str:
        parts = []
        try:
            entries, start, end = self.ms_entries()
            n = len(entries)
            due = " It's due today." if self.report_due_today() else ""
            parts.append(f"One: the weekly missed sales report. {plural(n, 'missed sale').capitalize()} in the last seven days.{due}")
        except StoreError as e:
            parts.append(f"One: the weekly missed sales report, though {e}.")
        try:
            missing, total = self.cost_missing()
            parts.append(f"Two: cost fixer. {missing:,} {'item needs' if missing == 1 else 'items need'} a cost.")
        except StoreError as e:
            parts.append(f"Two: cost fixer, though {e}.")
        parts.append("Three: liquidation. Check StockX and eBay for anything that's sold.")
        return "Three tasks on your list. " + " ".join(parts)
