"""Website usage from browser history (Chrome and Safari on this Mac).

Screen Time on macOS does not expose per-site time, so this module reads the
browsers' own history databases:

* **Chrome** — ``~/Library/Application Support/Google/Chrome/*/History``.
  Every visit has ``visit_duration``, but that counts the time a tab stays
  open, background tabs included, so it is far too high on its own.
* **Safari** — ``~/Library/Safari/History.db``. No duration. Visits with
  ``origin = 1`` were synced through iCloud from other devices (iPhone/iPad);
  they arrive only while Safari runs on this Mac.

Time per visit is an *estimate*: the gap to the next visit in the same stream
(browser + device), capped (Chrome: also by its own duration). Private
browsing is never written to history; ``screentime web`` reports it as the
difference between Safari's Screen Time minutes and the estimated history
minutes ("unaccounted").
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

UTC = timezone.utc

HOME = Path.home()
CHROME_ROOT = HOME / "Library/Application Support/Google/Chrome"
SAFARI_DB = HOME / "Library/Safari/History.db"

CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
COCOA_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

# Upper bound for one visit when the next visit is far away (idle, closed).
CAP_S = {"chrome": 10 * 60, "safari": 10 * 60}

# Screen Time bundle ids per browser stream: the app's real foreground time.
# History alone overcounts (auto-refreshing pages, tabs left open), so a
# stream with Screen Time data is scaled down to it; domains keep their share.
STREAM_BUNDLES = {
    "chrome/mac": ("com.google.Chrome",),
    "safari/mac": ("com.apple.Safari",),
    "safari/iphone": ("com.apple.mobilesafari",),
}

SAFARI_BUNDLES = {
    "mac": ("com.apple.Safari",),
    "iphone": ("com.apple.mobilesafari",),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_visits (
  visit_key TEXT PRIMARY KEY,
  browser TEXT NOT NULL,          -- chrome | safari
  device TEXT NOT NULL,           -- mac | iphone (safari origin=1)
  visited_at_utc TEXT NOT NULL,
  domain TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  raw_duration_s REAL,            -- chrome visit_duration, NULL for safari
  est_s REAL NOT NULL DEFAULT 0   -- estimated foreground time
);
CREATE INDEX IF NOT EXISTS idx_web_visits_time ON web_visits(visited_at_utc);
CREATE INDEX IF NOT EXISTS idx_web_visits_stream ON web_visits(browser, device, visited_at_utc);
"""


@dataclass(frozen=True)
class Visit:
    browser: str
    device: str
    at: datetime  # UTC
    domain: str
    title: str
    raw_duration_s: float | None
    native_id: str

    @property
    def key(self) -> str:
        return hashlib.sha1(
            f"{self.browser}:{self.device}:{self.native_id}:{self.at.isoformat()}".encode()
        ).hexdigest()


def domain_of(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host or None


def _snapshot(path: Path) -> Path:
    """Copy a live SQLite database (and its WAL) so the browser lock is no issue."""
    tmp = Path(tempfile.mkdtemp(prefix="screentime-web-"))
    target = tmp / path.name
    shutil.copy2(path, target)
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            shutil.copy2(side, tmp / (path.name + suffix))
    return target


def _open_snapshot(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_snapshot(path))
    conn.row_factory = sqlite3.Row
    return conn


def chrome_profiles(root: Path = CHROME_ROOT) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/History") if p.is_file())


def read_chrome(path: Path, since: datetime) -> list[Visit]:
    since_us = int((since - CHROME_EPOCH).total_seconds() * 1_000_000)
    conn = _open_snapshot(path)
    try:
        rows = conn.execute(
            """SELECT v.id, v.visit_time, v.visit_duration, u.url, u.title
            FROM visits v JOIN urls u ON u.id = v.url
            WHERE v.visit_time >= ? ORDER BY v.visit_time""",
            (since_us,),
        ).fetchall()
    finally:
        conn.close()
    profile = path.parent.name
    out = []
    for row in rows:
        domain = domain_of(row["url"])
        if not domain:
            continue
        out.append(
            Visit(
                browser="chrome",
                device="mac",
                at=CHROME_EPOCH + timedelta(microseconds=row["visit_time"]),
                domain=domain,
                title=row["title"] or "",
                raw_duration_s=(row["visit_duration"] or 0) / 1_000_000,
                native_id=f"{profile}:{row['id']}",
            )
        )
    return out


def read_safari(path: Path, since: datetime) -> list[Visit]:
    since_s = (since - COCOA_EPOCH).total_seconds()
    conn = _open_snapshot(path)
    try:
        rows = conn.execute(
            """SELECT v.id, v.visit_time, v.title, v.origin, i.url
            FROM history_visits v JOIN history_items i ON i.id = v.history_item
            WHERE v.visit_time >= ? AND v.load_successful = 1
            ORDER BY v.visit_time""",
            (since_s,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        domain = domain_of(row["url"])
        if not domain:
            continue
        out.append(
            Visit(
                browser="safari",
                device="iphone" if row["origin"] == 1 else "mac",
                at=COCOA_EPOCH + timedelta(seconds=row["visit_time"]),
                domain=domain,
                title=row["title"] or "",
                raw_duration_s=None,
                native_id=str(row["id"]),
            )
        )
    return out


def estimate(visits: list[Visit]) -> list[tuple[Visit, float]]:
    """Seconds per visit: gap to the next visit in the same stream, capped."""
    streams: dict[tuple[str, str], list[Visit]] = defaultdict(list)
    for visit in visits:
        streams[(visit.browser, visit.device)].append(visit)
    result: list[tuple[Visit, float]] = []
    for (browser, _), items in streams.items():
        items.sort(key=lambda v: v.at)
        cap = CAP_S[browser]
        for i, visit in enumerate(items):
            gap = (items[i + 1].at - visit.at).total_seconds() if i + 1 < len(items) else cap
            seconds = min(max(gap, 0.0), cap)
            if visit.raw_duration_s is not None:
                seconds = min(seconds, visit.raw_duration_s)
            result.append((visit, seconds))
    return result


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def sync(
    conn: sqlite3.Connection,
    *,
    chrome: Iterable[Path] | None = None,
    safari: Path | None = SAFARI_DB,
    lookback: timedelta = timedelta(days=3),
    full: bool = False,
) -> dict[str, Any]:
    """Import recent visits and re-estimate them. Idempotent.

    Re-reads ``lookback`` before the newest stored visit, because the
    estimate of the last visit changes when the next one arrives and Safari
    receives iCloud visits late.
    """
    init(conn)
    newest = conn.execute("SELECT MAX(visited_at_utc) FROM web_visits").fetchone()[0]
    if full or not newest:
        since = datetime(2000, 1, 1, tzinfo=UTC)
    else:
        since = datetime.fromisoformat(newest) - lookback
    visits: list[Visit] = []
    errors: list[str] = []
    for path in chrome if chrome is not None else chrome_profiles():
        try:
            visits += read_chrome(Path(path), since)
        except (OSError, sqlite3.Error) as exc:
            errors.append(f"{path}: {exc}")
    if safari is not None and Path(safari).exists():
        try:
            visits += read_safari(Path(safari), since)
        except (OSError, sqlite3.Error) as exc:
            errors.append(f"{safari}: {exc}")
    rows = [
        (
            v.key, v.browser, v.device, v.at.isoformat(), v.domain, v.title[:300],
            v.raw_duration_s, round(seconds, 3),
        )
        for v, seconds in estimate(visits)
    ]
    conn.executemany(
        """INSERT INTO web_visits(visit_key, browser, device, visited_at_utc, domain,
        title, raw_duration_s, est_s) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(visit_key) DO UPDATE SET est_s=excluded.est_s,
        title=excluded.title, raw_duration_s=excluded.raw_duration_s""",
        rows,
    )
    conn.commit()
    return {"web_visits_read": len(rows), "web_errors": errors}


# ---------------------------------------------------------------- report


def _day_range(day: date, tz: ZoneInfo) -> tuple[str, str]:
    start = datetime.combine(day, datetime.min.time(), tz).astimezone(UTC)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _safari_screen_time(conn: sqlite3.Connection, start: str, end: str) -> dict[str, float]:
    """Seconds of Safari per device kind from Screen Time sessions."""
    out: dict[str, float] = {}
    try:
        devices = {
            row[0]: row[1]
            for row in conn.execute("SELECT device_id, platform FROM devices")
        }
        for kind, bundles in SAFARI_BUNDLES.items():
            marks = ",".join("?" * len(bundles))
            rows = conn.execute(
                f"""SELECT device_id, SUM(duration_s) FROM screen_events
                WHERE bundle_id IN ({marks}) AND started_at_utc >= ? AND started_at_utc < ?
                GROUP BY device_id""",
                (*bundles, start, end),
            ).fetchall()
            total = sum(s for dev, s in rows if kind == "mac" or devices.get(dev) in ("iphone", "ipad"))
            out[kind] = total or 0.0
    except sqlite3.Error:
        pass
    return out


def _app_screen_time(conn: sqlite3.Connection, start: str, end: str) -> dict[str, float]:
    """Screen Time seconds per browser stream (mac vs iPhone by device platform)."""
    out: dict[str, float] = {}
    try:
        platforms = dict(conn.execute("SELECT device_id, platform FROM devices").fetchall())
        for stream, bundles in STREAM_BUNDLES.items():
            want_mac = stream.endswith("/mac")
            marks = ",".join("?" * len(bundles))
            for device_id, seconds in conn.execute(
                f"""SELECT device_id, SUM(duration_s) FROM screen_events
                WHERE bundle_id IN ({marks}) AND started_at_utc >= ? AND started_at_utc < ?
                GROUP BY device_id""",
                (*bundles, start, end),
            ):
                if (platforms.get(device_id) == "mac") == want_mac:
                    out[stream] = out.get(stream, 0.0) + (seconds or 0.0)
    except sqlite3.Error:
        pass
    return out


def report(conn: sqlite3.Connection, day: date, tz: ZoneInfo, top: int = 15) -> dict[str, Any]:
    start, end = _day_range(day, tz)
    rows = conn.execute(
        """SELECT browser, device, domain, SUM(est_s) AS s, COUNT(*) AS n
        FROM web_visits WHERE visited_at_utc >= ? AND visited_at_utc < ?
        GROUP BY browser, device, domain""",
        (start, end),
    ).fetchall()
    by_domain: dict[str, dict[str, Any]] = {}
    streams: dict[str, float] = defaultdict(float)
    for browser, device, domain, seconds, count in rows:
        item = by_domain.setdefault(domain, {"domain": domain, "est_s": 0.0, "visits": 0, "where": set()})
        item["est_s"] += seconds
        item["visits"] += count
        item["where"].add(f"{browser}/{device}")
        streams[f"{browser}/{device}"] += seconds
    top_domains = sorted(by_domain.values(), key=lambda d: -d["est_s"])[:top]
    for item in top_domains:
        item["where"] = sorted(item["where"])
        item["est_s"] = round(item["est_s"])
    # Scale streams that history overcounts to the browser's Screen Time.
    app_time = _app_screen_time(conn, start, end)
    scale = {
        stream: min(1.0, app_time[stream] / seconds)
        for stream, seconds in streams.items()
        if seconds > 0 and app_time.get(stream)
    }
    if scale:
        streams = defaultdict(float)
        rescaled: dict[str, dict[str, Any]] = {}
        for browser, device, domain, seconds, count in rows:
            stream = f"{browser}/{device}"
            seconds *= scale.get(stream, 1.0)
            streams[stream] += seconds
            item = rescaled.setdefault(domain, {"domain": domain, "est_s": 0.0, "visits": 0, "where": set()})
            item["est_s"] += seconds
            item["visits"] += count
            item["where"].add(stream)
        top_domains = sorted(rescaled.values(), key=lambda d: -d["est_s"])[:top]
        for item in top_domains:
            item["where"] = sorted(item["where"])
            item["est_s"] = round(item["est_s"])
    safari_st = _safari_screen_time(conn, start, end)
    unaccounted = {
        kind: round(max(0.0, safari_st.get(kind, 0.0) - streams.get(f"safari/{kind}", 0.0)))
        for kind in ("mac", "iphone")
    }
    return {
        "day": day.isoformat(),
        "timezone": str(tz),
        "total_est_s": round(sum(streams.values())),
        "streams": {k: round(v) for k, v in sorted(streams.items())},
        "top_domains": top_domains,
        "safari_screen_time_s": {k: round(v) for k, v in safari_st.items()},
        "scaled_to_screen_time": sorted(scale),
        "safari_unaccounted_s": unaccounted,
    }


def _hm(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def render(data: dict[str, Any], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)
    md = fmt == "markdown"
    lines = [f"## Websites {data['day']}" if md else f"Websites {data['day']} ({data['timezone']})"]
    lines.append(f"{'- **Total (estimate):**' if md else 'Total (estimate):'} {_hm(data['total_est_s'])}")
    for stream, seconds in data["streams"].items():
        lines.append(f"{'- ' if md else '  '}{stream}: {_hm(seconds)}")
    for kind, seconds in data["safari_unaccounted_s"].items():
        if seconds >= 60:
            lines.append(
                f"{'- ' if md else '  '}Safari {kind} without history "
                f"(private browsing or not synced yet): {_hm(seconds)}"
            )
    lines.append("")
    if md:
        lines += ["| Site | Time | Visits | Where |", "|---|---|---|---|"]
        for d in data["top_domains"]:
            lines.append(f"| {d['domain']} | {_hm(d['est_s'])} | {d['visits']} | {', '.join(d['where'])} |")
    else:
        for d in data["top_domains"]:
            lines.append(f"  {_hm(d['est_s']):>7}  {d['visits']:>4}  {d['domain']:<40} {', '.join(d['where'])}")
    return "\n".join(lines)


def _parse_day(value: str, tz: ZoneInfo) -> date:
    today = datetime.now(tz).date()
    if value == "today":
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    return date.fromisoformat(value)


def add_command(sub: Any) -> None:
    web = sub.add_parser("web", help="website time from Chrome/Safari history")
    web.add_argument("--day", default="today", help="today | yesterday | YYYY-MM-DD")
    web.add_argument("--top", type=int, default=15)
    web.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    web.add_argument("--sync", action="store_true", help="import browser history first")
    web.add_argument("--full", action="store_true", help="with --sync: import all history")


def run(args: argparse.Namespace, conn: sqlite3.Connection, tz: ZoneInfo) -> int:
    init(conn)
    if args.sync:
        result = sync(conn, full=args.full)
        for error in result["web_errors"]:
            print(f"screentime web: {error}")
    print(render(report(conn, _parse_day(args.day, tz), tz, args.top), args.format))
    return 0
