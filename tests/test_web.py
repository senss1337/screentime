from datetime import datetime, timedelta, timezone
import sqlite3

from screentime import web

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


def visit(minutes: float, domain: str, browser: str = "chrome", raw: float | None = 9999.0):
    return web.Visit(
        browser=browser,
        device="mac",
        at=T0 + timedelta(minutes=minutes),
        domain=domain,
        title="",
        raw_duration_s=raw if browser == "chrome" else None,
        native_id=f"{domain}:{minutes}",
    )


def test_domain_of_normalises_and_skips_non_http():
    assert web.domain_of("https://www.Example.com/path") == "example.com"
    assert web.domain_of("https://m.vk.com/") == "vk.com"
    assert web.domain_of("chrome://settings") is None


def test_estimate_uses_gap_cap_and_raw_duration():
    visits = [visit(0, "a.com"), visit(2, "b.com", raw=30.0), visit(60, "c.com")]
    seconds = {v.domain: s for v, s in web.estimate(visits)}
    assert seconds["a.com"] == 120  # gap to next visit
    assert seconds["b.com"] == 30  # chrome's own duration is shorter than the gap
    assert seconds["c.com"] == web.CAP_S["chrome"]  # last visit: cap


def test_report_scales_stream_to_screen_time():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """CREATE TABLE devices(device_id TEXT, platform TEXT);
        CREATE TABLE screen_events(device_id TEXT, bundle_id TEXT,
          started_at_utc TEXT, duration_s REAL);
        INSERT INTO devices VALUES ('mac:1', 'mac');
        INSERT INTO screen_events VALUES ('mac:1', 'com.google.Chrome',
          '2026-11-01T10:00:00+00:00', 300);"""
    )
    web.init(conn)
    rows = [(v.key, "chrome", "mac", v.at.isoformat(), v.domain, "", None, s)
            for v, s in web.estimate([visit(0, "a.com"), visit(5, "b.com"), visit(10, "a.com")])]
    conn.executemany("INSERT INTO web_visits VALUES (?,?,?,?,?,?,?,?)", rows)
    data = web.report(conn, T0.date(), web.ZoneInfo("UTC"))
    assert data["scaled_to_screen_time"] == ["chrome/mac"]
    assert data["total_est_s"] == 300
