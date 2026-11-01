"""This Mac's own app focus from Biome (``App.InFocus/local``).

Why: on macOS 26 knowledgeC's ``/app/usage`` stream can stop growing while
Screen Time keeps working (seen on 26.4.1: frozen at exactly 10 000 rows, the
rows written since the last reboot). Screen Time itself reads Biome, where
``App.InFocus/local`` holds the same focus transitions for this Mac as
``App.InFocus/remote/<uuid>`` does for an iPhone (same SEGB/protobuf format,
parsed by :mod:`screentime.sources.biome`).

How it fits with the ``knowledgec`` source (same device id,
``mac:<IOPlatformUUID>``, so both feed one device):

* While knowledgeC is alive (its newest ``/app/usage`` row is less than
  ``STALE_AFTER`` older than the newest Biome transition) this source emits
  nothing and keeps no state, so the two never double count.
* Once knowledgeC is stale, it emits intervals from knowledgeC's newest row on.
* With the ``knowledgec`` source disabled it emits everything.

Not usage: focus moves to ``loginwindow`` while the Mac is locked and to the
screen saver while it runs; those intervals are dropped. Focus also stays on
an app while the display sleeps, so intervals are cut by knowledgeC's
``/display/isBacklit`` "off" periods where that stream has data.
"""

from __future__ import annotations

import bisect
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sqlite3
from typing import Any

from . import Device, UsageEvent
from .biome import Open, _segments, read_segment, stitch
from .knowledgec import KnowledgeCSource, query_copy

log = logging.getLogger(__name__)

STREAM_LOCAL = Path("Biome/streams/restricted/App.InFocus/local")
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
STALE_AFTER = 6 * 3600.0  # seconds knowledgeC may lag Biome before we take over
NOT_USAGE = frozenset({"com.apple.loginwindow", "com.apple.ScreenSaver.Engine"})

LAST_USAGE_SQL = """
SELECT MAX(o.ZENDDATE) FROM ZOBJECT o LEFT JOIN ZSOURCE s ON s.Z_PK = o.ZSOURCE
WHERE o.ZSTREAMNAME = '/app/usage' AND s.ZDEVICEID IS NULL
"""
DISPLAY_OFF_SQL = """
SELECT o.ZSTARTDATE, o.ZENDDATE FROM ZOBJECT o LEFT JOIN ZSOURCE s ON s.Z_PK = o.ZSOURCE
WHERE o.ZSTREAMNAME = '/display/isBacklit' AND s.ZDEVICEID IS NULL
  AND o.ZVALUEINTEGER = 0 AND o.ZENDDATE > o.ZSTARTDATE AND o.ZENDDATE > ?
ORDER BY o.ZSTARTDATE
"""


def _utc(cf: float) -> datetime:
    return APPLE_EPOCH + timedelta(seconds=cf)


def subtract(
    start: float, end: float, gaps: list[tuple[float, float]], starts: list[float]
) -> list[tuple[float, float]]:
    """``[start, end)`` minus the sorted, non-overlapping ``gaps``."""
    pieces = []
    i = max(0, bisect.bisect_right(starts, start) - 1)
    cur = start
    while i < len(gaps) and gaps[i][0] < end:
        gap_start, gap_end = gaps[i]
        if gap_end > cur:
            if gap_start > cur:
                pieces.append((cur, min(gap_start, end)))
            cur = max(cur, gap_end)
            if cur >= end:
                break
        i += 1
    if cur < end:
        pieces.append((cur, end))
    return pieces


def _merge(gaps: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for gap_start, gap_end in sorted(gaps):
        if merged and gap_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], gap_end))
        else:
            merged.append((gap_start, gap_end))
    return merged


class MacBiomeSource:
    name = "biome_local"

    def __init__(
        self, library: Path, knowledgec_db: Path, *, after_knowledgec: bool = True
    ) -> None:
        self.library = Path(library)
        self.knowledgec_db = Path(knowledgec_db)
        self.after_knowledgec = after_knowledgec
        self._kc = KnowledgeCSource(self.knowledgec_db)

    def devices(self) -> list[Device]:
        if not (self.library / STREAM_LOCAL).is_dir():
            return []
        return [replace(d, source=self.name) for d in self._kc.devices()]

    def _knowledgec(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any] | None:
        if not self.knowledgec_db.exists():
            return None
        try:
            return query_copy(self.knowledgec_db, sql, params)
        except (OSError, sqlite3.Error) as exc:
            log.warning("biome_local: cannot read %s: %s", self.knowledgec_db, exc)
            return None

    def read(
        self, state: dict[str, Any] | None
    ) -> tuple[list[UsageEvent], dict[str, Any]]:
        state = dict(state or {})
        devices = self.devices()
        if not devices:
            return [], state
        device = devices[0]
        files = _segments(self.library / STREAM_LOCAL)
        if not files:
            return [], state

        prev = state.get("mac") or {}
        last = int(prev.get("file") or -1)
        carry: Open | None = None
        if any(int(p.name) == last for p in files):
            if prev.get("carry"):
                carry = (prev["carry"][0], float(prev["carry"][1]))
            files = [p for p in files if int(p.name) >= last]
        else:
            files = [p for p in files if int(p.name) > last]
        if not files:
            return [], state

        closed: list[tuple[str, float, float]] = []
        newest_carry = carry
        for path in files:
            newest_carry = carry
            piece, carry = stitch(read_segment(path), carry)
            closed += piece
        if not closed:
            return [], state

        floor: float | None = None
        if self.after_knowledgec:
            rows = self._knowledgec(LAST_USAGE_SQL)
            kc_last = rows[0][0] if rows and rows[0][0] is not None else None
            biome_last = closed[-1][2]
            if kc_last is not None and biome_last - kc_last < STALE_AFTER:
                return [], state  # knowledgeC is alive: it covers this Mac
            floor = kc_last

        cursor = prev.get("cursor")
        if floor is not None:
            cursor = max(cursor or floor, floor)
        first = closed[0][1] if cursor is None else max(closed[0][1], cursor)
        gaps = _merge(
            [(float(a), float(b)) for a, b in self._knowledgec(DISPLAY_OFF_SQL, (first,)) or []]
        )
        starts = [g[0] for g in gaps]

        events: list[UsageEvent] = []
        for bundle, start, end in closed:
            if cursor is not None and end <= cursor:
                continue  # emitted by an earlier run, or covered by knowledgeC
            if cursor is not None and start < cursor:
                start = cursor  # interval straddles the knowledgeC cut-off
            cursor = end
            if bundle in NOT_USAGE:
                continue
            for piece_start, piece_end in subtract(start, end, gaps, starts):
                if piece_end - piece_start >= 1.0:
                    events.append(
                        UsageEvent(device.id, bundle, _utc(piece_start), _utc(piece_end))
                    )
        state["mac"] = {
            "file": files[-1].name,
            "carry": list(newest_carry) if newest_carry else None,
            "cursor": cursor,
        }
        return events, state
