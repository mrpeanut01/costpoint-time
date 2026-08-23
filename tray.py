#!/usr/bin/env python3
"""
tray.py — menu-bar front end for the Costpoint timesheet automation.

Everything lives in one menu. The menu bar shows a single coloured dot:

    🟢  everything in this period is entered      🔴  a weekday is missing time
    🌴  today is PTO                              🎉  today is a holiday
    ⚪️  credentials not set yet                   🔄  talking to Costpoint

Click it and you get today's line, a clickable colour strip for the period, the
next scheduled run (which also holds "run it now" and the run time), and
credentials. Clicking a box in the strip cycles that day through
work → 🎉 holiday → 🌴 PTO; the carets on the right walk forward and back
through periods.

The strip is a real AppKit view inside the menu (NSMenuItem.setView_), which is
the only way to get a horizontal row of individually clickable cells — a plain
menu item is one row, one click.

No business logic lives here: reads and writes go through timesheet.py, intent
is stored in plan.py's plan.json, and the scheduled launchd run reads that same
plan. The tray can be quit at any time without affecting the daily automation.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import plan as planning

planning.load_env()

import objc
import rumps
from AppKit import (NSAttributedString, NSBezierPath, NSColor, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName,
                    NSTrackingActiveAlways, NSTrackingArea,
                    NSTrackingMouseEnteredAndExited, NSTrackingMouseMoved, NSView)
from Foundation import NSMakePoint, NSMakeRect

import timesheet as T
from costpoint_mobile import CostpointError, LoginError, MfaRequired, SamlRequired

SYNC_EVERY_SECONDS = 15 * 60
TICK_SECONDS = 1

ICON_OK = "🟢"
ICON_MISSING = "🔴"
ICON_PTO = "🌴"
ICON_HOLIDAY = "🎉"
ICON_UNSET = "⚪️"
ICON_BUSY = "🔄"
ICON_ERROR = "⚠️"

# strip geometry (points)
BOX_W, BOX_H, GAP = 13, 14, 3
LABEL_W, CARET_W, PAD = 44, 15, 8
STRIP_Y, HINT_Y, VIEW_H = 5, 23, 38      # boxes on top, hovered day beneath
MAX_DAYS = 16                      # semi-monthly periods run 15–16 days


def log(*a) -> None:
    print(f"[tray {datetime.now():%H:%M:%S}]", *a, flush=True)


# ── the one place that talks to Costpoint ─────────────────────────────────────
class Engine:
    """Serialises every Costpoint conversation behind one lock.

    Each operation logs in fresh (three JSON POSTs, a couple of seconds) rather
    than holding a session open — the tray sits idle for hours at a time and a
    stale session is a worse failure mode than a re-login.
    """

    MESSAGE_TTL = 25          # seconds a status line stays up before the summary returns

    def __init__(self):
        self.lock = threading.Lock()
        self.busy = False
        self._message = ""
        self._message_at = 0.0
        self.status = planning.Status.load()
        self.plan = planning.Plan.load()
        self.pending: dict[str, str] = {}      # iso -> target, until the write lands
        self.config = planning.Config.load()

    @property
    def message(self) -> str:
        return self._message

    @message.setter
    def message(self, value: str) -> None:
        self._message, self._message_at = value, time.monotonic()

    @property
    def message_fresh(self) -> bool:
        return bool(self._message) and (time.monotonic() - self._message_at) < self.MESSAGE_TTL

    # ── read ──────────────────────────────────────────────────────────────────
    def sync(self, history: bool = False) -> None:
        """Refresh the cached day colours from the live current period."""
        with self.lock:
            self.busy, self.message = True, "Syncing…"
            try:
                cp = T.connect_and_login()
                ts = T.TimesheetAutomation(cp)
                period = ts.load_current_period(create_if_missing=False)
                if period is None:
                    self.message = "No timesheet open for this period yet."
                else:
                    self._record(period)
                if history:
                    self._load_year(ts, period)
                self.status.error = None
                self.status.updated = datetime.now().isoformat(timespec="seconds")
                self.status.save()
                if period is not None:
                    self.message = f"Synced {period.period_no}/{period.year}."
                log(self.message)
            except (LoginError, MfaRequired, SamlRequired) as e:
                self._fail(f"Sign-in failed: {e}")
            except CostpointError as e:
                self._fail(f"Costpoint error: {e}")
            except Exception as e:                       # network down, etc.
                self._fail(f"{type(e).__name__}: {e}")
                log(traceback.format_exc())
            finally:
                self.busy = False

    def _fail(self, msg: str) -> None:
        self.message = msg
        self.status.error = msg
        self.status.updated = datetime.now().isoformat(timespec="seconds")
        self.status.save()
        log("ERROR:", msg)

    def _record(self, period: T.Period) -> None:
        """Fold one loaded period into the status cache."""
        days = sorted(period.day_map)
        if not days:
            return
        key = f"{period.year}-{period.schedule}-{period.period_no}"
        signed = (period.status_cd or "") in ("S", "P")
        self.status.periods[key] = {
            "start": days[0], "end": days[-1], "status": period.status_cd or "",
            "signed": signed, "period_no": period.period_no, "year": period.year,
            "entered": period.total_entered(), "expected": period.expected_hours(),
        }
        for iso, idx in period.day_map.items():
            entries = period.hours_on(idx)
            hours = sum(h for _, h in entries)
            label = ", ".join((ln.get("LINE_DESC") or ln.get("UDT02_ID") or "?")
                              for ln, _ in entries)
            udt02 = entries[0][0].get("UDT02_ID", "") if entries else ""
            self.status.days[iso] = {"hours": hours, "label": label,
                                     "udt02": udt02, "signed": signed}

    def _load_year(self, ts: T.TimesheetAutomation, current: T.Period | None) -> None:
        """Best effort: pull every period of the current year so past months get
        real colours instead of grey. Guarded — if this tenant doesn't answer an
        unfiltered header query we just keep the current period."""
        year = (current.year if current else None) or str(date.today().year)
        try:
            cp = ts.cp
            env = cp.api([
                cp.open_rs(ts.app, "", T.Rs.HEADER),
                cp.query_rs_data(ts.app, "", T.Rs.HEADER, ts.hdr_ctx,
                                 sort=[cp.sort_by("PERIOD_NO_CD", "asc")],
                                 where=[cp.query_cond("YEAR_NO_CD", year)]),
                cp.get_rs_data(ts.app, "", T.Rs.HEADER, ts.hdr_ctx, row_start=0, row_end=99),
            ])
            headers = [T._flat(r) for r in T._rows(env, "getRSData")]
            log(f"history: {len(headers)} period header(s) for {year}")
            for h in headers:
                codes = (h.get("TS_SCHEDULE_CD"), h.get("YEAR_NO_CD"), h.get("PERIOD_NO_CD"))
                if not all(codes) or (current and codes[2] == current.period_no
                                      and codes[1] == current.year):
                    continue
                self._record(T.Period(h, ts._read_lines(h, *codes)))
        except Exception as e:
            log(f"history load skipped: {type(e).__name__}: {e}")

    # ── write ─────────────────────────────────────────────────────────────────
    def fill(self, day: date, charge: str | None = None, replace: bool = False,
             hours: float = T.HOURS) -> dict:
        """Enter one day for real. `replace` overwrites hours already on the day
        (the 9 AM run filled it, and you've since decided to take PTO)."""
        with self.lock:
            self.busy, self.message = True, f"Writing {day:%b %d}…"
            try:
                res = T.fill_day(day, charge=charge, write=True, replace=replace,
                                 hours=hours)
                self.message = res["message"]
                log("write:", res["message"])
                return res
            except (LoginError, MfaRequired, SamlRequired) as e:
                msg = f"Sign-in failed: {e}"
            except CostpointError as e:
                msg = f"Costpoint error: {e}"
            except SystemExit as e:                      # timesheet.py exits on save failure
                msg = f"Save failed (exit {e.code}) — see the log."
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                log(traceback.format_exc())
            finally:
                self.busy = False
            self.message = msg
            log("ERROR:", msg)
            return {"ok": False, "message": msg}

    def discover_charges(self) -> dict:
        """Read the account's charge favourites and let the config work out which
        is which. Runs after a successful sign-in, and on demand from the menu."""
        with self.lock:
            self.busy, self.message = True, "Reading your charge codes…"
            try:
                cp = T.connect_and_login()
                ts = T.TimesheetAutomation(cp)
                period = ts.load_current_period(create_if_missing=False)
                if period is None:
                    msg = "No open timesheet to read charges from yet."
                    self.message = msg
                    return {"ok": False, "message": msg}
                favorites = T.load_favorites(cp, ts, period)
                self.config = planning.Config.load()
                unresolved = self.config.apply_favorites(favorites)
                self.config.save()
                T.reload_config()
                if unresolved:
                    msg = (f"Found {len(favorites)} charges — pick a "
                           f"{'/'.join(unresolved)} charge under Charges.")
                else:
                    msg = f"Charges set from your {len(favorites)} favourites."
                self.message = msg
                log(msg)
                return {"ok": not unresolved, "message": msg}
            except Exception as e:
                msg = f"Couldn't read charges: {e}"
                self.message = msg
                log(msg)
                return {"ok": False, "message": msg}
            finally:
                self.busy = False

    def test_login(self) -> dict:
        with self.lock:
            self.busy, self.message = True, "Checking sign-in…"
            try:
                cp = T.connect_and_login()
                who = os.environ.get("COSTPOINT_USERNAME", "")
                self.message = f"Signed in as {who}."
                try:
                    cp.logout()
                except Exception:
                    pass
                return {"ok": True, "message": self.message}
            except MfaRequired as e:
                msg = f"Password accepted, but this account needs an MFA code: {e}"
            except (LoginError, SamlRequired) as e:
                msg = str(e)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
            finally:
                self.busy = False
            self.message = msg
            return {"ok": False, "message": msg}


# ── periods ───────────────────────────────────────────────────────────────────
def current_period(eng: Engine, today: date | None = None) -> dict | None:
    iso = (today or date.today()).isoformat()
    for p in eng.status.periods.values():
        if p["start"] <= iso <= p["end"]:
            return p
    return None


def _semi_monthly(around: date) -> dict:
    """Fallback period boundaries for a tenant on a 1st–15th / 16th–EOM schedule,
    used for months Costpoint hasn't given us a header for yet."""
    if around.day <= 15:
        start, end = around.replace(day=1), around.replace(day=15)
    else:
        nxt = (around.replace(day=28) + timedelta(days=4)).replace(day=1)
        start, end = around.replace(day=16), nxt - timedelta(days=1)
    return {"start": start.isoformat(), "end": end.isoformat(), "signed": False,
            "period_no": None, "entered": None, "expected": None}


def window_at(eng: Engine, offset: int) -> dict:
    """The period `offset` steps from the one containing today. Known periods
    come from the sync cache; beyond it we extrapolate the semi-monthly pattern
    so you can still plan PTO into next year."""
    known = sorted(eng.status.periods.values(), key=lambda p: p["start"])
    today = date.today()
    here = next((i for i, p in enumerate(known) if p["start"] <= today.isoformat() <= p["end"]), None)
    if here is None:
        known, here = [_semi_monthly(today)], 0
    i = here + offset
    if 0 <= i < len(known):
        return known[i]
    # walk outwards from the nearest known edge
    win = known[-1] if i >= len(known) else known[0]
    step = 1 if i >= len(known) else -1
    for _ in range(abs(i - (len(known) - 1 if step > 0 else 0))):
        edge = date.fromisoformat(win["end"]) + timedelta(days=1) if step > 0 \
            else date.fromisoformat(win["start"]) - timedelta(days=1)
        win = _semi_monthly(edge)
    return win


def window_days(win: dict) -> list[date]:
    d, end, out = date.fromisoformat(win["start"]), date.fromisoformat(win["end"]), []
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


# ── one click on a day ────────────────────────────────────────────────────────
CYCLE_NEXT = {"normal": "holiday", "holiday": "pto", "pto": "normal"}


def plan_state(view: dict) -> str:
    """Which of the three cycle positions a day is currently in.

    The plan is consulted first because it updates the instant you click, while
    what's entered in Costpoint only catches up after a sync. Falling back to the
    entered charge covers a day that was filled without ever being planned.
    """
    if view["pto"]:
        return "pto"
    if view["holiday"] or view["unconfirmed"]:
        return "holiday"
    if view["state"] == "pto":
        return "pto"
    if view["state"] in ("holiday", "unsure"):
        return "holiday"
    return "normal"


def _cache_day(eng: Engine, d: date, hours: float, kind: str | None) -> None:
    """Update the cached day the moment a write succeeds, so the cell repaints
    straight away instead of waiting on the next sync — otherwise a quick second
    click would read the old colour and skip a step in the cycle."""
    cfg = planning.Config.load()
    charge = cfg.charge({"normal": "work"}.get(kind, kind)) if kind else None
    eng.status.days[d.isoformat()] = {
        "hours": hours,
        "label": (charge or {}).get("label", ""),
        "udt02": (charge or {}).get("udt02", ""),
        "signed": eng.status.signed(d),
    }
    eng.status.save()


def apply_day_state(eng: Engine, d: date, target: str, sync_after: bool = True) -> dict:
    """Move a day to 'normal' | 'holiday' | 'pto' — the strip's click action.

    Nothing is pre-filed. A future day only records intent in plan.json; the
    scheduled run reads it and enters the right charge when the day comes round.
    Only a day that has already arrived is written now, because for that day
    normal time entry has been and gone.

    The exception is *removing* hours: if a future day already carries time (say
    it was pre-filed before this rule, or entered by hand in Costpoint), that time
    is cleared, so the plan and the timesheet can't disagree.
    """
    if d.weekday() >= 5:
        return {"ok": False, "message": "Weekends aren't charged."}
    if eng.status.signed(d):
        return {"ok": False,
                "message": f"{d:%b %d} is in a signed period — change it in Costpoint."}

    federal = planning.federal_holidays(d.year)
    eng.plan.set_state(d, target, federal)
    eng.plan.save()

    today = date.today()
    p = current_period(eng)
    in_open = bool(p) and p["start"] <= d.isoformat() <= p["end"]
    word = {"pto": "PTO", "holiday": "Holiday", "normal": "the working charge"}[target]

    if d > today:
        entry = eng.status.days.get(d.isoformat()) or {}
        if in_open and float(entry.get("hours") or 0) > 0:
            res = eng.fill(d, charge="capture", replace=True, hours=0.0)
            if res["ok"]:
                _cache_day(eng, d, 0.0, None)
            if sync_after:
                eng.sync()
            if not res["ok"]:
                return {"ok": False, "message": res["message"]}
            return {"ok": True,
                    "message": f"{d:%b %d} → {word}; cleared the hours already on it."}
        return {"ok": True, "message": f"{d:%b %d} → {word}; entered on the day."}

    if not in_open:
        return {"ok": True, "message": f"{d:%b %d} → {word}; outside the open period."}

    # The day has arrived, so write it. Always replace: the cached day may be
    # stale, and "skip if it already has hours" would silently do nothing while
    # reporting success.
    charge = None if target == "normal" else target
    res = eng.fill(d, charge=charge, replace=True)
    if res["ok"]:
        _cache_day(eng, d, T.HOURS, target)
    if sync_after:
        eng.sync()
    if not res["ok"]:
        return {"ok": False, "message": res["message"]}
    return {"ok": True, "message": f"{d:%b %d} → {word} in Costpoint."}


# ── the clickable colour strip ────────────────────────────────────────────────
_OWNER: "TrayApp | None" = None            # the single TrayApp, for the NSView
_HOVER: str | None = None                  # ISO date the pointer is over


def _color(state: str):
    return {
        "filled":  NSColor.systemGreenColor(),
        "missing": NSColor.systemRedColor(),
        "pto":     NSColor.systemPurpleColor(),
        "holiday": NSColor.systemOrangeColor(),
        "unsure":  NSColor.systemOrangeColor(),
    }.get(state)                            # None → an empty "white" box


def _text(s: str, x: float, y: float, size: float = 10, color=None, bold=False) -> None:
    font = (NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    attrs = {NSFontAttributeName: font,
             NSForegroundColorAttributeName: color or NSColor.secondaryLabelColor()}
    NSAttributedString.alloc().initWithString_attributes_(s, attrs).drawAtPoint_(
        NSMakePoint(x, y))


def _centered(s: str, rect, size: float, color, bold: bool = True) -> None:
    """Draw a short glyph centred in a cell, measuring it first so it sits true
    regardless of the system font's metrics."""
    font = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
    attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: color}
    text = NSAttributedString.alloc().initWithString_attributes_(s, attrs)
    box = text.size()
    text.drawAtPoint_(NSMakePoint(rect.origin.x + (rect.size.width - box.width) / 2.0,
                                  rect.origin.y + (rect.size.height - box.height) / 2.0))


def hover_text(d: date, v: dict) -> str:
    """The line shown under the strip while the pointer is over a day."""
    when = f"{d:%a %b %-d}"
    if v["state"] == "weekend":
        return f"{when}  ·  weekend"
    if v["entered"]:
        return f"{when}  ·  {T.hours_str(v['hours'])}h {v['label']}"
    if v["state"] == "pto":
        return f"{when}  ·  PTO"
    if v["state"] in ("holiday", "unsure"):
        return f"{when}  ·  {v['holiday_name'] or 'Holiday'}"
    if v["state"] == "missing":
        return f"{when}  ·  no time entered"
    if v["state"] == "future":
        return f"{when}  ·  nothing entered yet"
    return f"{when}  ·  not synced"


class StripView(NSView):
    """A row of day cells inside the menu. Draws from live state every time the
    menu opens, so it never needs invalidating on a timer."""

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    # ── layout ────────────────────────────────────────────────────────────────
    @objc.python_method
    def layout_boxes(self):
        """[(x, date, view), …] plus the two caret rects. Shared by draw and
        hit-test so a click can never land on a different box than it looks."""
        app = _OWNER
        win = window_at(app.engine, app.offset)
        days = window_days(win)
        federal = planning.federal_holidays(days[0].year, days[-1].year)
        today = date.today()
        x0 = PAD + LABEL_W + 6
        boxes = []
        for i, d in enumerate(days):
            v = planning.day_view(d, app.engine.plan, app.engine.status, federal, today,
                                  app.engine.pending, app.engine.config)
            boxes.append((x0 + i * (BOX_W + GAP), d, v))
        end_x = x0 + len(days) * (BOX_W + GAP) + 3
        prev_x = end_x + LABEL_W
        return win, days, boxes, end_x, prev_x, prev_x + CARET_W

    @objc.python_method
    def box_at(self, x: float):
        """The (date, view) under an x position, or None."""
        for bx, d, v in self.layout_boxes()[2]:
            if bx - GAP / 2 <= x <= bx + BOX_W + GAP / 2:
                return d, v
        return None

    # ── drawing ───────────────────────────────────────────────────────────────
    def drawRect_(self, rect):
        # AppKit swallows exceptions from drawing/tracking, which would leave a
        # blank menu row and no clue why. Log instead.
        try:
            self.draw_strip()
        except Exception:
            log("strip draw failed:\n" + traceback.format_exc())

    @objc.python_method
    def draw_strip(self):
        if _OWNER is None:
            return
        win, days, boxes, end_x, prev_x, next_x = self.layout_boxes()
        y = STRIP_Y
        start_d, end_d = days[0], days[-1]

        _text(f"{start_d:%b %-d}", PAD, y + 2)
        _text(f"{end_d:%b %-d}" + (" 🔒" if win.get("signed") else ""), end_x + 6, y + 2)

        self.removeAllToolTips()
        for x, d, v in boxes:
            # Weekends are drawn as a thin rule rather than a cell: they aren't
            # chargeable, so they shouldn't look like something you can click.
            cell = NSMakeRect(x, y, BOX_W, BOX_H)
            self.addToolTipRect_owner_userData_(cell, self, None)
            if v["state"] == "weekend":
                bar = NSMakeRect(x, y + BOX_H / 2 - 1.5, BOX_W, 3)
                rule = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bar, 1.5, 1.5)
                NSColor.labelColor().colorWithAlphaComponent_(0.14).set()
                rule.fill()
                continue

            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(cell, 3, 3)
            fill = _color(v["state"])
            if fill is not None:
                # Solid once plan and timesheet agree; outlined while a change is
                # still on its way to Costpoint.
                if v["entered"]:
                    fill.set()
                    path.fill()
                    ink = NSColor.whiteColor()
                else:
                    fill.colorWithAlphaComponent_(0.28).set()
                    path.fill()
                    fill.set()
                    path.setLineWidth_(1.2)
                    path.stroke()
                    ink = fill
                letter = {"holiday": "H", "unsure": "H", "pto": "P"}.get(v["state"])
                if letter:
                    _centered(letter, cell, 9, ink)
            else:                                   # an empty day — click to cycle it
                NSColor.labelColor().colorWithAlphaComponent_(0.16).set()
                path.fill()
                NSColor.labelColor().colorWithAlphaComponent_(0.38).set()
                path.setLineWidth_(1.0)
                path.stroke()
            if d == date.today():
                NSColor.labelColor().set()
                path.setLineWidth_(1.8)
                path.stroke()
            if _HOVER == d.isoformat():
                NSColor.labelColor().colorWithAlphaComponent_(0.55).set()
                ring = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(x - 1.5, y - 1.5, BOX_W + 3, BOX_H + 3), 4, 4)
                ring.setLineWidth_(1.0)
                ring.stroke()

        if _OWNER.offset != 0:
            _text("‹", prev_x, y - 1, size=13, color=NSColor.labelColor())
        _text("›", next_x, y - 1, size=13, color=NSColor.labelColor())

        hint = next((hover_text(d, v) for _, d, v in boxes if d.isoformat() == _HOVER), "")
        if hint:
            attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(10),
                     NSForegroundColorAttributeName: NSColor.secondaryLabelColor()}
            a = NSAttributedString.alloc().initWithString_attributes_(hint, attrs)
            a.drawAtPoint_(NSMakePoint((self.bounds().size.width - a.size().width) / 2.0, HINT_Y))

    # ── hover ─────────────────────────────────────────────────────────────────
    def updateTrackingAreas(self):
        for area in list(self.trackingAreas()):
            self.removeTrackingArea_(area)
        self.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved | NSTrackingActiveAlways,
            self, None))
        objc.super(StripView, self).updateTrackingAreas()

    def mouseMoved_(self, event):
        self.track(event)

    def mouseEntered_(self, event):
        self.track(event)

    def mouseExited_(self, event):
        global _HOVER
        if _HOVER is not None:
            _HOVER = None
            self.display()

    @objc.python_method
    def track(self, event):
        global _HOVER
        try:
            p = self.convertPoint_fromView_(event.locationInWindow(), None)
            hit = self.box_at(p.x)
            iso = hit[0].isoformat() if hit else None
            if iso != _HOVER:
                _HOVER = iso
                self.display()                    # menus defer normal redraws
        except Exception:
            log("strip hover failed:\n" + traceback.format_exc())

    def view_stringForToolTip_point_userData_(self, view, tag, point, data):
        hit = self.box_at(point.x)
        return hover_text(*hit) if hit else ""

    # ── clicking ──────────────────────────────────────────────────────────────
    def mouseDown_(self, event):
        try:
            self.handle_click(event)
        except Exception:
            log("strip click failed:\n" + traceback.format_exc())

    @objc.python_method
    def handle_click(self, event):
        if _OWNER is None:
            return
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        win, days, boxes, end_x, prev_x, next_x = self.layout_boxes()
        if p.x >= next_x - 3:
            _OWNER.shift(1)
        elif _OWNER.offset != 0 and p.x >= prev_x - 3:
            _OWNER.shift(-1)
        else:
            for x, d, v in boxes:
                if x - GAP / 2 <= p.x <= x + BOX_W + GAP / 2:
                    _OWNER.day_clicked(d, v)
                    break
        # display(), not setNeedsDisplay_(): while a menu is tracking, the normal
        # redraw cycle doesn't run, so a deferred repaint never happens and the
        # click looks like it did nothing.
        self.display()


# ── menu bar ──────────────────────────────────────────────────────────────────
class TrayApp(rumps.App):
    def __init__(self):
        super().__init__(ICON_UNSET, quit_button=None)
        global _OWNER
        _OWNER = self
        self.engine = Engine()
        self.offset = 0                    # which period the strip is showing
        self._jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._day_worker, daemon=True).start()

        self.i_today = rumps.MenuItem("Starting…")
        self.i_period = rumps.MenuItem("")
        self.i_strip = rumps.MenuItem("period-strip")
        width = PAD * 2 + LABEL_W * 2 + 12 + MAX_DAYS * (BOX_W + GAP) + CARET_W * 2
        self.strip_view = StripView.alloc().initWithFrame_(NSMakeRect(0, 0, width, VIEW_H))
        self.i_strip._menuitem.setView_(self.strip_view)

        self.i_next = rumps.MenuItem("⏰  Next run")
        self.i_creds = rumps.MenuItem("🔑  Credentials")
        self.i_charges = rumps.MenuItem("🏷  Charges")
        self.menu = [self.i_today, self.i_strip, self.i_period, None,
                     self.i_next, None,
                     self.i_creds, self.i_charges,
                     rumps.MenuItem("📄  Open log", callback=self.open_log), None,
                     rumps.MenuItem("Quit", callback=rumps.quit_application)]
        self.build_next_menu()
        self.build_creds_menu()
        self.build_charges_menu()

        # Federal holidays are pre-filled so they show up already marked; days the
        # user has explicitly set either way are never touched.
        if self.engine.plan.seed_federal(date.today().year):
            self.engine.plan.save()
            log("pre-filled US federal holidays in", planning.PLAN_PATH)

        rumps.Timer(self.tick, TICK_SECONDS).start()
        rumps.Timer(self.background_sync, SYNC_EVERY_SECONDS).start()
        if planning.have_credentials() and planning.Config.load().host:
            self.spawn(self._verify_and_sync)
        else:
            self.engine.message = "Add your Costpoint server and sign-in to get started."

    # ── helpers ───────────────────────────────────────────────────────────────
    def spawn(self, fn) -> None:
        """Run a Costpoint call off the AppKit main thread; the timer repaints."""
        threading.Thread(target=fn, daemon=True).start()

    def later(self, fn, delay: float = 0.15) -> None:
        """Run `fn` on the main thread once the menu has closed. Menu tracking
        blocks the default run loop mode, so a timer scheduled here fires the
        moment tracking ends — which is exactly when a dialog can be shown."""
        def fire(timer):
            timer.stop()
            fn()
        rumps.Timer(fire, delay).start()

    # ── strip interaction ─────────────────────────────────────────────────────
    def shift(self, delta: int) -> None:
        self.offset = max(-24, min(24, self.offset + delta))

    def day_clicked(self, d: date, view: dict) -> None:
        if d.weekday() >= 5:
            return
        if self.engine.status.signed(d):
            self.engine.message = f"{d:%b %d} is in a signed period — change it in Costpoint."
            return
        if not planning.have_credentials():
            self.engine.message = "Set your credentials first."
            return
        # Straight through the cycle, no dialog: every position is reachable and
        # reversible by clicking again, and the worker coalesces a fast cycle
        # into one write.
        self.queue_day(d, CYCLE_NEXT[plan_state(view)])

    def queue_day(self, d: date, target: str) -> None:
        """Record the intent immediately (so the strip repaints at once) and hand
        the Costpoint write to the serial worker."""
        self.engine.plan.set_state(d, target, planning.federal_holidays(d.year))
        self.engine.plan.save()
        self.engine.pending[d.isoformat()] = target
        self._jobs.put((d, target))

    def _day_worker(self) -> None:
        """One worker, so day changes can't land out of order. A short settle
        window collapses a fast click-click-click cycle into a single write."""
        while True:
            first = self._jobs.get()
            time.sleep(0.6)
            pending = {first[0]: first[1]}
            try:
                while True:
                    d, t = self._jobs.get_nowait()
                    pending[d] = t
            except queue.Empty:
                pass
            for d, t in pending.items():
                self.announce(apply_day_state(self.engine, d, t, sync_after=False))
            self.engine.sync()
            for d in pending:                    # settled — the cache is authoritative again
                self.engine.pending.pop(d.isoformat(), None)

    def announce(self, res: dict) -> None:
        self.engine.message = res.get("message", "")
        log("day:", res.get("message"))

    # ── next run ──────────────────────────────────────────────────────────────
    def build_next_menu(self) -> None:
        if len(self.i_next):
            self.i_next.clear()
        self.i_next.add(rumps.MenuItem("⚡︎  Run it now", callback=self.run_now))
        self.i_next.add(rumps.MenuItem("🔄  Sync now", callback=self.sync_now))
        self.i_next.add(rumps.separator)
        header = rumps.MenuItem("Run weekdays at")
        self.i_next.add(header)                       # no callback → a section label
        sched = planning.read_schedule()
        for hour in range(6, 19):
            item = rumps.MenuItem("        " + planning.fmt_time(hour, 0),
                                  callback=self.set_hour)
            item.state = 1 if sched and sched[0] == hour and sched[1] == 0 else 0
            self.i_next.add(item)
        self.i_next.add(rumps.MenuItem("        Other time…", callback=self.set_custom_time))

    def refresh_next_title(self) -> None:
        sched = planning.read_schedule()
        if sched is None:
            self.i_next.title = "⏰  Next run · not scheduled (run ./deploy.sh)"
            return
        nxt = planning.next_run(*sched)
        when = "today" if nxt.date() == date.today() else f"{nxt:%a %b %-d}"
        self.i_next.title = f"⏰  Next run · {when} at {planning.fmt_time(*sched)}"

    def set_hour(self, sender) -> None:
        hour = int(sender.title.strip().split(":")[0])
        if "PM" in sender.title and hour != 12:
            hour += 12
        if "AM" in sender.title and hour == 12:
            hour = 0
        self.apply_schedule(hour, 0)

    def set_custom_time(self, _=None) -> None:
        sched = planning.read_schedule() or (9, 0)
        win = rumps.Window("What time should the daily run fire? (e.g. 9:30 AM or 14:00)",
                           "Daily run time", planning.fmt_time(*sched),
                           ok="Set", cancel="Cancel", dimensions=(200, 24))
        res = win.run()
        if not res.clicked:
            return
        parsed = self.parse_time(res.text)
        if parsed is None:
            rumps.alert("Didn't understand that time", "Try 9:30 AM, 09:30 or 14:00.")
            return
        self.apply_schedule(*parsed)

    @staticmethod
    def parse_time(text: str) -> tuple[int, int] | None:
        t = (text or "").strip().upper().replace(".", "")
        pm, am = t.endswith("PM"), t.endswith("AM")
        t = t.removesuffix("PM").removesuffix("AM").strip()
        try:
            hh, _, mm = t.partition(":")
            hour, minute = int(hh), int(mm or 0)
        except ValueError:
            return None
        if pm and hour != 12:
            hour += 12
        if am and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour, minute

    def apply_schedule(self, hour: int, minute: int) -> None:
        try:
            self.engine.message = planning.write_schedule(hour, minute)
            log(self.engine.message)
        except Exception as e:
            # Bind the text now: Python unbinds `e` at the end of the except
            # block, and this alert is deliberately deferred until the menu closes.
            msg = str(e)
            self.engine.message = msg
            self.later(lambda: rumps.alert("Couldn't change the schedule", msg))
        self.build_next_menu()
        self.refresh_next_title()

    def run_now(self, _=None) -> None:
        if not self.require_credentials():
            return
        today = date.today()
        if today.weekday() >= 5:
            self.later(lambda: rumps.alert("It's the weekend", "Nothing to file today."))
            return
        self.spawn(lambda: (self.engine.fill(today), self.engine.sync()))

    def sync_now(self, _=None) -> None:
        if not self.require_credentials():
            return
        self.spawn(lambda: self.engine.sync(history=True))

    # ── credentials ───────────────────────────────────────────────────────────
    def build_creds_menu(self) -> None:
        c = planning.read_credentials()
        cfg = planning.Config.load()
        if len(self.i_creds):
            self.i_creds.clear()
        self.i_creds.add(rumps.MenuItem(f"Server · {cfg.host or '—'}",
                                        callback=self.edit_host))
        self.i_creds.add(rumps.MenuItem(f"Organization · {c['organization'] or '—'}",
                                        callback=self.edit_org))
        self.i_creds.add(rumps.MenuItem(f"Username · {c['username'] or '—'}",
                                        callback=self.edit_user))
        self.i_creds.add(rumps.MenuItem(f"Password · {'••••••••' if c['password'] else 'not set'}",
                                        callback=self.edit_password))
        self.i_creds.add(rumps.separator)
        self.i_creds.add(rumps.MenuItem("Test sign-in", callback=self.test_login))

    def _prompt(self, title: str, message: str, default: str, secure: bool = False) -> str | None:
        win = rumps.Window(message, title, default, ok="Save", cancel="Cancel",
                           dimensions=(260, 24), secure=secure)
        res = win.run()
        return res.text.strip() if res.clicked else None

    def build_charges_menu(self) -> None:
        """Working / Holiday / PTO, each listing the account's own favourites.
        Costpoint flags the leave charges, so normally there is nothing to do
        here — it's for an account with more than one working charge."""
        cfg = planning.Config.load()
        self.config = cfg
        if len(self.i_charges):
            self.i_charges.clear()
        if not cfg.favorites:
            self.i_charges.add(rumps.MenuItem("Sign in to discover your charges"))
        for kind, title in (("work", "Working"), ("holiday", "Holiday"), ("pto", "PTO")):
            chosen = cfg.charge(kind)
            parent = rumps.MenuItem(f"{title} · {chosen['label'] if chosen else 'not set'}")
            options = [f for f in cfg.favorites
                       if (kind == "holiday" and f["holiday"])
                       or (kind == "pto" and f["vacation"])
                       or (kind == "work" and not f["holiday"] and not f["vacation"])]
            for fav in options or cfg.favorites:
                item = rumps.MenuItem(fav["label"], callback=self.pick_charge(kind, fav))
                item.state = 1 if chosen and chosen["udt02"] == fav["udt02"] else 0
                parent.add(item)
            self.i_charges.add(parent)
        self.i_charges.add(rumps.separator)
        self.i_charges.add(rumps.MenuItem("Refresh from Costpoint", callback=self.refresh_charges))

    def pick_charge(self, kind: str, fav: dict):
        def cb(_=None):
            cfg = planning.Config.load()
            cfg.set_charge(kind, fav["udt02"], fav["label"])
            cfg.save()
            T.reload_config()
            self.engine.message = f"{kind.capitalize()} charge set to {fav['label']}."
            log(self.engine.message)
            self.build_charges_menu()
        return cb

    def refresh_charges(self, _=None) -> None:
        if not self.require_credentials():
            return
        self.spawn(lambda: (self.engine.discover_charges(), self.build_charges_menu()))

    def edit_host(self, _=None) -> None:
        self.later(lambda: self._save_host())

    def _save_host(self) -> None:
        cfg = planning.Config.load()
        value = self._prompt("Costpoint server", "The host your Costpoint Time & "
                             "Expense runs on, e.g. yourcompany-cp.costpointfoundations.com",
                             cfg.host)
        if value is None:
            return
        cfg.host = value.replace("https://", "").replace("http://", "").strip("/ ")
        cfg.save()
        T.reload_config()
        log("server set to", cfg.host)
        self.build_creds_menu()
        self.spawn(self._verify_and_sync)

    def edit_org(self, _=None) -> None:
        self.later(lambda: self._save_cred("organization"))

    def edit_user(self, _=None) -> None:
        self.later(lambda: self._save_cred("username"))

    def edit_password(self, _=None) -> None:
        self.later(lambda: self._save_cred("password"))

    def _save_cred(self, field: str) -> None:
        c = planning.read_credentials()
        prompts = {
            "organization": ("Costpoint organization", "e.g. ACMECORPLLC", False),
            "username": ("Costpoint username", "e.g. 12345.J.DOE", False),
            "password": ("Costpoint password", "Stored in .env, mode 600. "
                         "Change or reset it in Costpoint first — the API has no "
                         "password-change call.", True),
        }
        title, msg, secure = prompts[field]
        value = self._prompt(title, msg, "" if secure else c[field], secure)
        if value is None or (secure and not value):
            return
        c[field] = value
        planning.save_credentials(c["organization"], c["username"], c["password"])
        log("credentials updated:", field)
        self.build_creds_menu()
        self.spawn(lambda: self._verify_and_sync())

    def _verify_and_sync(self) -> None:
        res = self.engine.test_login()
        if not res["ok"]:
            return
        # A working sign-in is the first chance to learn the account's charges.
        if not planning.Config.load().is_complete():
            self.engine.discover_charges()
            self.build_charges_menu()
        self.engine.sync(history=True)

    def test_login(self, _=None) -> None:
        if not self.require_credentials():
            return
        self.spawn(lambda: self.later_alert(self.engine.test_login()))

    def later_alert(self, res: dict) -> None:
        self.engine.message = res["message"]
        log("sign-in test:", res["message"])

    def require_credentials(self) -> bool:
        if planning.have_credentials() and planning.Config.load().host:
            return True
        self.later(lambda: rumps.alert(
            "No credentials yet", "Set your Costpoint organization, username and "
            "password under Credentials."))
        return False

    # ── repaint (main thread, once a second) ──────────────────────────────────
    def tick(self, _=None) -> None:
        eng = self.engine
        today = date.today()
        federal = planning.federal_holidays(today.year)
        eng.plan = planning.Plan.load()
        view = planning.day_view(today, eng.plan, eng.status, federal, today)

        if eng.busy:
            self.title = ICON_BUSY
        elif not planning.have_credentials() or not planning.Config.load().is_complete():
            self.title = ICON_UNSET
        elif eng.status.error:
            self.title = ICON_ERROR
        elif view["state"] in ("holiday", "unsure"):
            self.title = ICON_HOLIDAY
        elif view["state"] == "pto":
            self.title = ICON_PTO
        elif view["state"] == "unknown":
            self.title = ICON_UNSET
        elif view["state"] in ("filled", "weekend"):
            self.title = ICON_MISSING if self.any_missing() else ICON_OK
        else:
            self.title = ICON_MISSING

        if view["state"] == "filled":
            line = f"Today · {today:%a %b %-d} — {T.hours_str(view['hours'])}h {view['label']}"
        elif view["state"] == "weekend":
            line = f"Today · {today:%a %b %-d} — weekend"
        elif view["state"] in ("holiday", "unsure"):
            line = f"Today · {today:%a %b %-d} — {view['holiday_name']}"
        elif view["state"] == "pto":
            line = f"Today · {today:%a %b %-d} — PTO (not entered yet)"
        elif view["state"] == "unknown":
            line = f"Today · {today:%a %b %-d} — not synced yet"
        else:
            line = f"Today · {today:%a %b %-d} — no time entered"
        self.i_today.title = line

        p = current_period(eng)
        if eng.busy or eng.status.error or eng.message_fresh:
            summary = eng.message or ""
        elif p:
            state = "signed ✓" if p.get("signed") else \
                f"{T.hours_str(p.get('entered', 0))}/{T.hours_str(p.get('expected', 0))}h"
            summary = f"Period {p.get('period_no')}  ·  {state}"
        else:
            summary = "Not synced yet"
        self.i_period.title = summary
        self.refresh_next_title()
        self.strip_view.setNeedsDisplay_(True)

    def any_missing(self) -> bool:
        """Any weekday in the current period that should have time and doesn't."""
        p = current_period(self.engine)
        if not p:
            return False
        federal = planning.federal_holidays(date.today().year)
        for d in window_days(p):
            if planning.day_view(d, self.engine.plan, self.engine.status,
                                 federal)["state"] == "missing":
                return True
        return False

    def background_sync(self, _=None) -> None:
        if planning.have_credentials() and not self.engine.busy:
            self.spawn(self.engine.sync)

    def open_log(self, _=None) -> None:
        if not os.path.exists(planning.LOG_PATH):
            open(planning.LOG_PATH, "a").close()
        subprocess.run(["open", planning.LOG_PATH], check=False)


def single_instance_or_exit():
    """Hold an exclusive lock for the life of the process so a second launch
    (e.g. the launchd agent starting while a hand-run copy is up) bows out
    instead of putting a second icon in the menu bar."""
    import fcntl
    os.makedirs(planning.APP_DIR, exist_ok=True)
    fh = open(os.path.join(planning.APP_DIR, "tray.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Exit 0, not 1: the launchd agent has KeepAlive/SuccessfulExit=false, so
        # a non-zero exit here would restart it forever against a hand-run copy.
        print("Another copy of the tray app is already running; exiting.")
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh                       # keep it open — closing releases the lock


if __name__ == "__main__":
    _lock = single_instance_or_exit()
    TrayApp().run()
