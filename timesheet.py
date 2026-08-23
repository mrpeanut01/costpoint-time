#!/usr/bin/env python3
"""
timesheet.py — daily Costpoint timesheet automation via the Mobile REST API.

Business logic: 8h on "Capture & Proposal Dev." every weekday, "Holiday" on US
federal holidays. Leave charges (Holiday / PTO) are added as a new line if not
already on the period.

A handful of JSON POSTs instead of a headless browser. Built on
costpoint_mobile.py; wire details in docs/API_PROTOCOL.md.

The write path (putRSData + saveApp [+ sign]) modifies the real timesheet, so it
only runs with --save (or --sign).

Usage:
  python timesheet.py                 # dry run: load live, show what WOULD change
  python timesheet.py --save          # write today's hours (no sign)
  python timesheet.py --charge pto --date 2026-06-08 --save
  python timesheet.py --save --sign   # write hours AND sign/submit the period
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime


import plan as planning

planning.load_env()

import holidays

from costpoint_mobile import (CostpointMobile, Rs, LoginError, MfaRequired,
                              SamlRequired, CostpointError)

# New-line sentinel rowNo the app uses for an unsaved line.
NEW_LINE_ROW_NO = -59999

# Everything tenant-specific lives in config.json (see plan.Config) and is
# discovered at setup, never hardcoded: the server, the length of a working day,
# and the three charge codes. `udt02` is the project id (DFLT_UDT02_ID from
# TMMTS_CHARGE_FAVE) that, set on a new line + validateField, resolves the full
# charge (account/org/paytype).
CONFIG = planning.Config.load()
HOURS = CONFIG.hours_per_day
HOST = CONFIG.host
WORK_LABEL = HOLIDAY_LABEL = PTO_LABEL = ""
CHARGES: dict = {}
LEAVE_LABELS: set = set()
CHARGE_CHOICES: dict = {}


def reload_config() -> "planning.Config":
    """Re-read config.json into the module-level names. The tray calls this after
    a setting changes so a running process picks it up without a restart."""
    global CONFIG, HOURS, HOST, WORK_LABEL, HOLIDAY_LABEL, PTO_LABEL
    global CHARGES, LEAVE_LABELS, CHARGE_CHOICES
    CONFIG = planning.Config.load()
    HOURS = CONFIG.hours_per_day
    HOST = CONFIG.host
    WORK_LABEL = CONFIG.label("work")
    HOLIDAY_LABEL = CONFIG.label("holiday")
    PTO_LABEL = CONFIG.label("pto")
    CHARGES = {CONFIG.label(k): {"udt02": CONFIG.charge(k)["udt02"]}
               for k in CONFIG.KINDS if CONFIG.charge(k)}
    LEAVE_LABELS = {HOLIDAY_LABEL, PTO_LABEL}   # exempt from the future-day guard
    CHARGE_CHOICES = {"auto": None, "work": WORK_LABEL, "holiday": HOLIDAY_LABEL,
                      "pto": PTO_LABEL, "capture": WORK_LABEL}   # 'capture' kept as an alias
    return CONFIG


reload_config()


# ── business logic ────────────────────────────────────────────────────────────
def line_label_for(d: date, federal, plan: "planning.Plan | None" = None) -> str:
    """Charge for a day. The tray app's plan wins over the federal calendar: a day
    marked PTO becomes PTO, a federal holiday the user declined becomes a normal
    working day, and a company holiday the user added becomes Holiday."""
    if plan is not None:
        if plan.is_pto(d):
            return PTO_LABEL
        return HOLIDAY_LABEL if plan.is_holiday(d, federal) else WORK_LABEL
    return HOLIDAY_LABEL if d in federal else WORK_LABEL


def hours_str(h: float) -> str:
    """Match Costpoint's formatting: whole numbers without trailing decimals."""
    return str(int(h)) if float(h).is_integer() else f"{h:g}"


def _to_hours(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ── response helpers ──────────────────────────────────────────────────────────
def _resp(env: dict, method: str) -> dict:
    for r in env.get("responses", []):
        if method in r:
            return r[method]
    return {}


def _rows(env: dict, method: str) -> list:
    return _resp(env, method).get("rsData", [])


def _flat(row: dict) -> dict:
    out = {"rowNo": row.get("rowNo")}
    for kv in row.get("data", []):
        out.update(kv)
    return out


class Period:
    """A loaded timesheet period: header fields, DAYn↔date map, and lines."""
    def __init__(self, header: dict, lines: list[dict]):
        self.header = header
        self.lines = lines
        self.row_no = header.get("rowNo")
        self.schedule = header.get("TS_SCHEDULE_CD")
        self.year = header.get("YEAR_NO_CD")
        self.period_no = header.get("PERIOD_NO_CD")
        self.status_cd = header.get("S_STATUS_CD")
        self.end_dt = (header.get("END_DT") or "")[:10]
        self.day_map = self._build_day_map(header)            # {'2026-06-05': 5, ...}
        self.date_for = {v: k for k, v in self.day_map.items()}  # {5: '2026-06-05'}

    @staticmethod
    def _build_day_map(header: dict) -> dict[str, int]:
        """{'2026-06-05': 5, ...} from DAYn_LABEL fields like 'Fri<BR>06/05/26'."""
        out = {}
        for k, v in header.items():
            m = re.fullmatch(r"DAY(\d+)_LABEL", k)
            if m and isinstance(v, str):
                dm = re.search(r"(\d{2})/(\d{2})/(\d{2})", v)
                if dm:
                    out[f"20{dm.group(3)}-{dm.group(1)}-{dm.group(2)}"] = int(m.group(1))
        return out

    def day_index_for(self, d: date) -> int | None:
        return self.day_map.get(d.isoformat())

    def find_line(self, label: str) -> dict | None:
        """Match a line for `label` by exact description or by its charge project
        id (leave lines come back as 'LEAVE - HOLIDAY', not 'Holiday')."""
        charge = CHARGES.get(label)
        for ln in self.lines:
            if label in (ln.get("LINE_DESC", ""), ln.get("UDT02_NAME", "")):
                return ln
            if charge and ln.get("UDT02_ID") == charge["udt02"]:
                return ln
        return None

    def hours_on(self, day_index: int) -> list[tuple[dict, float]]:
        """[(line, hrs), ...] for lines with hours > 0 on the given day."""
        out = []
        for ln in self.lines:
            hrs = _to_hours(ln.get(f"DAY{day_index}_HRS"))
            if hrs > 0:
                out.append((ln, hrs))
        return out

    def total_entered(self) -> float:
        """Total hours across the whole period (every line, every day)."""
        tot = 0.0
        for ln in self.lines:
            for k, v in ln.items():
                if re.fullmatch(r"DAY\d+_HRS", k):
                    tot += _to_hours(v)
        return tot

    def weekday_dates(self) -> list[date]:
        days = []
        for iso in self.day_map:
            d = datetime.strptime(iso, "%Y-%m-%d").date()
            if d.weekday() < 5:
                days.append(d)
        return sorted(days)

    def expected_hours(self, per_day: float = HOURS) -> float:
        return len(self.weekday_dates()) * per_day

    def final_working_day(self) -> date | None:
        wd = self.weekday_dates()
        return wd[-1] if wd else None


class TimesheetAutomation:
    def __init__(self, cp: CostpointMobile, proxy: bool = False):
        self.cp = cp
        self.app = Rs.TIMESHEET_APPROVE if proxy else Rs.TIMESHEET
        self.hdr_ctx = ".0" if proxy else "."
        self._next_new_row = NEW_LINE_ROW_NO
        self.server_today: str | None = None

    # ── constants / current period ────────────────────────────────────────────
    @staticmethod
    def _constants(env: dict) -> dict:
        out = {}
        for kv in _resp(env, "openApp").get("metaData", {}).get("constants", []):
            out.update(kv)
        return out

    def _line_ctx(self, period: Period) -> str:
        return (f".{period.row_no}" if self.hdr_ctx == "."
                else f".0.{period.row_no}")

    def load_current_period(self, create_if_missing: bool = False) -> Period | None:
        """Load the period that contains *today*, using the current-period codes
        from openApp metadata (works even before any time is entered). Returns
        None if the timesheet for the period doesn't exist yet and we're not
        creating it (dry run)."""
        cp = self.cp
        env = cp.api([cp.open_app(self.app)])
        c = self._constants(env)
        self.server_today = (c.get("CP_CURRENT_DATENOTIME") or "")[:10] or None
        codes = (c.get("TM_DFLT_TS_SCHEDULE_CODE"), c.get("TM_CUR_YEAR_NO_CD"),
                 c.get("TM_CUR_PERIOD_NO_CD"))
        if not all(codes):
            raise CostpointError(f"openApp didn't return current-period codes: {codes}")
        header = self._read_header(*codes)
        if header is None:
            if not create_if_missing:
                return None
            header = self._create_timesheet()
        lines = self._read_lines(header, *codes)
        return Period(header, lines)

    def _where(self, schedule, year, period_no):
        cp = self.cp
        return [cp.query_cond("TS_SCHEDULE_CD", schedule),
                cp.query_cond("YEAR_NO_CD", year),
                cp.query_cond("PERIOD_NO_CD", period_no)]

    def _read_header(self, schedule, year, period_no) -> dict | None:
        cp = self.cp
        env = cp.api([
            cp.open_rs(self.app, "", Rs.HEADER),
            cp.query_rs_data(self.app, "", Rs.HEADER, self.hdr_ctx,
                             where=self._where(schedule, year, period_no)),
            cp.get_rs_data(self.app, "", Rs.HEADER, self.hdr_ctx),
        ])
        rows = _rows(env, "getRSData")
        return _flat(rows[0]) if rows else None

    def _read_lines(self, header: dict, schedule, year, period_no) -> list[dict]:
        cp = self.cp
        ctx = (f".{header['rowNo']}" if self.hdr_ctx == "."
               else f".0.{header['rowNo']}")
        env = cp.api([
            cp.open_rs(self.app, Rs.HEADER, Rs.LINE),
            cp.get_rs_metadata(self.app, Rs.HEADER, Rs.LINE, ctx),
            cp.query_rs_data(self.app, Rs.HEADER, Rs.LINE, ctx,
                             sort=[cp.sort_by("LINE_NO", "asc")],
                             where=self._where(schedule, year, period_no)),
            cp.get_rs_data(self.app, Rs.HEADER, Rs.LINE, ctx),
        ])
        return [_flat(r) for r in _rows(env, "getRSData")]

    def _create_timesheet(self) -> dict:
        """Create the timesheet for the current period when it doesn't exist yet
        (first day of a period). Mirrors the app's runOpenTimesheetPeriodAction:
        put a new TMMTS header + runAction TMMTS_OPEN_TIMESHEET, then read it back.
        NOT yet exercised live (the current period already exists)."""
        cp = self.cp
        env = cp.api([
            cp.put_rs_data(self.app, "", Rs.HEADER, self.hdr_ctx,
                           [{"rowNo": NEW_LINE_ROW_NO, "status": ["new"], "data": []}]),
            cp.run_action(self.app, "TMMTS_OPEN_TIMESHEET", "", Rs.HEADER,
                          self.hdr_ctx, row_no="0"),
            cp.get_rs_data(self.app, "", Rs.HEADER, self.hdr_ctx,
                           row_start=NEW_LINE_ROW_NO, row_end=59998),
        ])
        if _resp(env, "runAction").get("respCode") != 0:
            raise CostpointError(f"Could not open/create the timesheet: {json.dumps(env)[:300]}")
        rows = _rows(env, "getRSData")
        if not rows:
            raise CostpointError("Timesheet created but no header returned.")
        print("  created the timesheet for the current period.")
        return _flat(rows[0])

    # ── lines ─────────────────────────────────────────────────────────────────
    def ensure_line(self, period: Period, label: str):
        """Return (line_row_no, is_new). Use an existing line if present, else
        stage a new line for any known charge (Capture/Holiday/PTO)."""
        existing = period.find_line(label)
        if existing is not None:
            return existing["rowNo"], False
        charge = CHARGES.get(label)
        if not charge:
            raise CostpointError(
                f"No line for '{label}' and no known charge to add it. Existing: "
                f"{[l.get('LINE_DESC') for l in period.lines]}")
        return self._stage_new_line(period, charge["udt02"]), True

    def _stage_new_line(self, period: Period, udt02_id: str) -> int:
        """Replicate the app's add-favorite flow (uncommitted until saveApp):
        createNewLine → set UDT02_ID → validateField (server resolves the
        charge). Returns the new line's rowNo. Verified live for Holiday/PTO."""
        cp, ctx = self.cp, self._line_ctx(period)
        new_row_no = self._next_new_row
        self._next_new_row -= 1                       # distinct sentinel per new line
        env = cp.api([
            cp.put_rs_data(self.app, Rs.HEADER, Rs.LINE, ctx,
                           [{"rowNo": new_row_no, "status": ["new"], "data": []}]),
            cp.run_action(self.app, "TMMTS_NEW_TS_LINE", Rs.HEADER, Rs.LINE, ctx,
                          row_no=new_row_no),
            cp.get_rs_data(self.app, Rs.HEADER, Rs.LINE, ctx,
                           row_start=new_row_no, row_end=new_row_no + 1),
        ])
        if _resp(env, "runAction").get("respCode") != 0:
            raise CostpointError(f"Could not create new line: {json.dumps(env)[:300]}")
        env = cp.api([
            cp.put_rs_data(self.app, Rs.HEADER, Rs.LINE, ctx,
                           [cp.rs_row(new_row_no, "updated",
                                      {"UDT02_ID": udt02_id, "ADD_FAVORITES_FL": "Y"})]),
            cp.validate_field(self.app, Rs.HEADER, Rs.LINE, ctx, new_row_no,
                              object_id="UDT02_ID"),
            cp.get_rs_data(self.app, Rs.HEADER, Rs.LINE, ctx,
                           row_start=new_row_no, row_end=new_row_no + 1),
        ])
        rows = _rows(env, "getRSData")
        resolved = _flat(rows[0]) if rows else {}
        if not resolved.get("CHARGE_CD"):
            raise CostpointError(
                f"New line did not resolve a charge for {udt02_id}: "
                f"{json.dumps(_resp(env, 'validateField'))[:300]}")
        print(f"  staged new line: {resolved.get('LINE_DESC')} "
              f"({resolved.get('CHARGE_CD')}) rowNo {new_row_no}")
        return new_row_no

    # ── save / sign ───────────────────────────────────────────────────────────
    def save_lines(self, period: Period, specs: list[dict], sign: bool = False,
                   explanation: str | None = None) -> list[dict]:
        """Build a save batch for one or more line edits. Each spec:
        {row_no, is_new, day_index, hours, udt02 (for new)}. With sign=True the
        header carries ACTION_CD='S'.

        `explanation` fills EXPLANATION_TEXT on the header. Costpoint refuses to
        save a change to hours it has already stored without one
        (TMMTIMESHEET_REV_EXPL_REQ, "Explanation or Reject Reason is required") —
        the mobile app pops a dialog and re-saves with the text on the TMMTS row.
        Adding hours to an empty day doesn't need it; revising does."""
        cp = self.cp
        line_rows = []
        for s in specs:
            fields = {f"DAY{s['day_index']}_HRS": hours_str(s["hours"])}
            if s.get("is_new") and s.get("udt02"):
                fields = {"UDT02_ID": s["udt02"], "ADD_FAVORITES_FL": "Y", **fields}
            line_rows.append(cp.rs_row(s["row_no"], "new" if s.get("is_new") else "updated", fields))
        header_fields = {"ACTION_CD": "S"} if sign else {"ACTION_CD": "", "S_STATUS_CD": period.status_cd or ""}
        if explanation:
            header_fields["EXPLANATION_TEXT"] = explanation[:250]
        return [
            cp.put_rs_data(self.app, Rs.HEADER, Rs.LINE, self._line_ctx(period), line_rows),
            cp.put_rs_data(self.app, "", Rs.HEADER, self.hdr_ctx,
                           [cp.rs_row(period.row_no, "updated", header_fields)]),
            cp.save_app(self.app, "1"),
        ]

    def sign_requests(self, period: Period) -> list[dict]:
        cp = self.cp
        return [
            cp.put_rs_data(self.app, "", Rs.HEADER, self.hdr_ctx,
                           [cp.rs_row(period.row_no, "updated", {"ACTION_CD": "S"})]),
            cp.save_app(self.app, "1"),
        ]


def load_favorites(cp: CostpointMobile, ts: "TimesheetAutomation",
                   period: Period) -> list[dict]:
    """The account's charge favourites, which is where the charge codes come from.

    Costpoint marks which favourite is the holiday charge and which is the
    vacation/PTO charge, so setup can work the rest out on its own — see
    plan.Config.apply_favorites.
    """
    env = cp.api([
        cp.open_rs(ts.app, Rs.HEADER, Rs.CHARGE_FAVE),
        cp.query_rs_data(ts.app, Rs.HEADER, Rs.CHARGE_FAVE, ts._line_ctx(period),
                         sort=[cp.sort_by("SEQ_NO", "desc")],
                         where=[cp.query_cond("DFLT_UDT02_ID", "", "is not null")]),
        cp.get_rs_data(ts.app, Rs.HEADER, Rs.CHARGE_FAVE, ts._line_ctx(period)),
    ])
    out = []
    for row in (_flat(r) for r in _rows(env, "getRSData")):
        udt02 = row.get("DFLT_UDT02_ID")
        if not udt02:
            continue
        out.append({"udt02": udt02,
                    "label": row.get("CHARGE_DESC") or row.get("CHARGE_CD") or udt02,
                    "holiday": row.get("HOLIDAY_FL") == "Y",
                    "vacation": row.get("VACATION_FL") == "Y"})
    return out


def connect_and_login(verbose=False) -> CostpointMobile:
    cfg = reload_config()
    if not cfg.host:
        print("ERROR: no Costpoint server configured. Set it in the tray app "
              "(Credentials → Server) or COSTPOINT_HOST in .env.", file=sys.stderr)
        sys.exit(2)
    system = os.environ.get("COSTPOINT_ORGANIZATION")
    user = os.environ.get("COSTPOINT_USERNAME")
    password = os.environ.get("COSTPOINT_PASSWORD")
    if not (system and user and password):
        print("ERROR: set COSTPOINT_ORGANIZATION / COSTPOINT_USERNAME / "
              "COSTPOINT_PASSWORD in .env or the environment.", file=sys.stderr)
        sys.exit(2)
    cp = CostpointMobile(cfg.host, system, verbose=verbose)
    print(f"Handshake… server version {cp.handshake()}")
    try:
        cp.login(user, password)
        print(f"Logged in as {user}.")
    except MfaRequired as mfa:
        if mfa.help_msg:
            print(f"MFA: {mfa.help_msg}")
        code = os.environ.get("COSTPOINT_MFA_CODE", "")
        pin = os.environ.get("COSTPOINT_MFA_PIN", "")
        if not code:
            if not sys.stdin.isatty():
                # Unattended (CI/cron): can't prompt for a time-limited passcode.
                print("\nMFA required but no COSTPOINT_MFA_CODE provided and no TTY "
                      "to prompt. A one-time passcode can't be a static secret; if "
                      "the scheduled account starts requiring MFA, switch it to an "
                      "MFA-exempt service account or app password.", file=sys.stderr)
                sys.exit(1)
            code = input("One-time passcode: ").strip()
        if mfa.want_pin and not pin and sys.stdin.isatty():
            pin = input("MFA PIN: ").strip()
        cp.login_mfa(code, pin)
        print("MFA accepted.")
    except SamlRequired as e:
        print(f"\nThis account uses SSO/SAML: {e}\nThe loginSaml flow isn't built "
              "yet — see docs/API_PROTOCOL.md.", file=sys.stderr)
        sys.exit(1)
    except LoginError as e:
        print(f"\nLOGIN REJECTED: {e}\nConfirm COSTPOINT_PASSWORD in .env is current.",
              file=sys.stderr)
        sys.exit(1)
    return cp


def _commit(cp, ts, period, reqs, what: str, write: bool) -> bool:
    """Send a save batch (or print it in dry run). Returns True on success."""
    if not write:
        print(f"\n--- DRY RUN: would {what}; nothing written ---")
        return True
    save = _resp(cp.api(reqs), "saveApp")
    if save.get("respCode") == 0:
        print(f"Saved — {what}.")
        return True
    print(f"Save FAILED ({what}) respCode {save.get('respCode')}: "
          f"{json.dumps(save)[:500]}", file=sys.stderr)
    return False


def _enter_day(cp, ts, period: Period, day: date, federal, charge_override, hours: float,
               write: bool, plan: "planning.Plan | None" = None,
               replace: bool = False) -> tuple[Period, bool]:
    """Enter hours for a single day on the current period. Skips a day that
    already has time (e.g. manually-entered PTO) unless `replace` is set, in
    which case the day's existing lines are zeroed and the new charge takes the
    hours — that's how the tray app turns a day the 9 AM run already filled into
    PTO. After a real write, reloads the period so staged lines/hours are visible
    to the next day in a self-heal sweep. Returns the (possibly reloaded) period;
    exits non-zero on save failure."""
    day_index = period.day_index_for(day)
    label = charge_override or line_label_for(day, federal, plan)

    todays = period.hours_on(day_index)
    if hours == 0 and not todays:                 # nothing to clear
        print(f"{day:%b %d} has no time on it.")
        return period, False
    if todays and not replace:
        desc = ", ".join(f"{ln.get('LINE_DESC', ln.get('UDT02_ID'))}={hours_str(h)}h"
                         for ln, h in todays)
        print(f"{day:%b %d} already has time entered: {desc}. Skipping entry.")
        return period, False

    def resolve(lbl):
        """(row_no, is_new) for `lbl` — stages a new line only when writing; in a
        dry run returns a placeholder so we don't mutate the server session."""
        ln = period.find_line(lbl)
        if ln is not None:
            return ln["rowNo"], False
        if lbl not in CHARGES:
            raise CostpointError(f"No line for '{lbl}' and no known charge to add it.")
        if write:
            return ts._stage_new_line(period, CHARGES[lbl]["udt02"]), True
        return NEW_LINE_ROW_NO, True

    # Holiday day → 8h holiday AND ensure the Capture line exists (0h).
    specs = []
    row_no, is_new = resolve(label)
    # Replacing: zero every *other* line that holds hours on this day, in the
    # same save batch, so the day never transiently reads as 16h.
    replaced = []
    if replace:
        for ln, h in todays:
            if ln["rowNo"] == row_no:
                continue
            specs.append({"row_no": ln["rowNo"], "is_new": False,
                          "day_index": day_index, "hours": 0.0})
            replaced.append(f"{ln.get('LINE_DESC', ln.get('UDT02_ID'))} {hours_str(h)}h -> 0h")
    specs.append({"row_no": row_no, "is_new": is_new, "day_index": day_index,
                  "hours": hours, "udt02": CHARGES.get(label, {}).get("udt02")})
    if label == HOLIDAY_LABEL and period.find_line(WORK_LABEL) is None:
        cap_row, cap_new = resolve(WORK_LABEL)          # add the 0h working line
        specs.append({"row_no": cap_row, "is_new": cap_new, "day_index": day_index,
                      "hours": 0.0, "udt02": CHARGES[WORK_LABEL]["udt02"]})
    what = (f"enter {hours_str(hours)}h on '{label}'"
            + (f" (replacing {', '.join(replaced)})" if replaced else "")
            + (f" + add the '{WORK_LABEL}' line"
               if len(specs) - len(replaced) > 1 else "")
            + f" for {day:%b %d}")
    # Revising hours Costpoint already holds needs a written explanation.
    explanation = (f"Corrected {day:%b %d} to {label} ({'; '.join(replaced)})"
                   if replaced else None)
    if not _commit(cp, ts, period, ts.save_lines(period, specs, explanation=explanation),
                   what, write):
        sys.exit(1)
    if write:
        period = ts.load_current_period(create_if_missing=False) or period
    return period, True


def fill_day(day: date, charge: str | None = None, hours: float | None = None,
             write: bool = True, verbose: bool = False, replace: bool = False) -> dict:
    """Log in, load the current period and enter one day. The tray app's write
    path; the CLI keeps using main(). `charge` is a CHARGE_CHOICES key (None =
    auto, i.e. the plan's PTO/holiday choices then the working charge).

    Returns {ok, label, message}. Raises whatever connect_and_login raises so the
    caller can show a real auth error (bad password, MFA, SSO).
    """
    reload_config()
    hours = HOURS if hours is None else hours
    federal = holidays.US(years=range(day.year, day.year + 2))
    plan = planning.Plan.load()
    override = CHARGE_CHOICES.get(charge or "auto")
    label = override or line_label_for(day, federal, plan)
    if day.weekday() >= 5:
        return {"ok": False, "label": label, "message": f"{day:%b %d} is a weekend."}

    cp = connect_and_login(verbose=verbose)
    ts = TimesheetAutomation(cp)
    period = ts.load_current_period(create_if_missing=write)
    if period is None:
        return {"ok": False, "label": label,
                "message": "No timesheet exists for the current period yet."}
    if period.day_index_for(day) is None:
        return {"ok": False, "label": label,
                "message": f"{day:%b %d} is outside the current period (ends {period.end_dt}); "
                           "it will be entered by the scheduled run once that period opens."}
    if period.status_cd in ("S", "P"):
        return {"ok": False, "label": label,
                "message": f"Period {period.period_no} is already signed — change it in Costpoint."}
    _, wrote = _enter_day(cp, ts, period, day, federal, override, hours, write, plan, replace)
    if not wrote:
        entries = period.hours_on(period.day_index_for(day))
        have = ", ".join(f"{ln.get('LINE_DESC', ln.get('UDT02_ID'))} {hours_str(h)}h"
                         for ln, h in entries)
        return {"ok": False, "label": label,
                "message": f"{day:%b %d} already has {have} — left alone."}
    return {"ok": True, "label": label,
            "message": f"{hours_str(hours)}h on '{label}' for {day:%b %d}."}


def main(argv=None):
    p = argparse.ArgumentParser(description="Costpoint daily timesheet via Mobile REST API")
    p.add_argument("--charge", choices=CHARGE_CHOICES, default="auto",
                   help="Charge to fill (default: auto = holiday on federal holidays, else capture).")
    p.add_argument("--hours", type=float, default=HOURS, help="Hours (default 8).")
    p.add_argument("--save", action="store_true", help="Commit writes (without it, dry run).")
    p.add_argument("--dry-run", action="store_true",
                   help="Load and report only; never write.")
    p.add_argument("--no-sign", action="store_true",
                   help="Never sign, even on the final working day of the period.")
    p.add_argument("--sign-now", action="store_true",
                   help="Force the end-of-period audit + sign on this run (if hours check out).")
    p.add_argument("--date", help="Target day YYYY-MM-DD (default: today).")
    p.add_argument("--allow-future", action="store_true",
                   help="Permit a target day after today (normally blocked for the working charge).")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    dry = args.dry_run
    write = args.save and not dry

    target = (datetime.strptime(args.date, "%Y-%m-%d").date()
              if args.date else date.today())
    federal = holidays.US(years=range(target.year, target.year + 2))
    plan = planning.Plan.load()          # PTO / holiday choices from the tray app
    label = CHARGE_CHOICES[args.charge] or line_label_for(target, federal, plan)
    holiday_name = federal.get(target, "") if plan.is_holiday(target, federal) else ""
    if plan.is_pto(target):
        holiday_name = "PTO (planned in the tray app)"
    print(f"{target:%A %B %d, %Y}"
          f"{' — ' + holiday_name if holiday_name else ''} → '{label}' ({hours_str(args.hours)}h)")
    if target.weekday() >= 5:
        print("Weekend — nothing to do.")
        return

    if target > date.today() and label not in LEAVE_LABELS and not args.allow_future:
        print(f"Refusing to fill '{label}' on a future day ({target}). "
              "Use --allow-future to override.", file=sys.stderr)
        sys.exit(1)

    cp = connect_and_login(verbose=args.verbose)
    ts = TimesheetAutomation(cp)

    # Load the period containing today (current-period codes from openApp); on the
    # first day of a period the timesheet may not exist yet — create it if writing.
    period = ts.load_current_period(create_if_missing=write)
    if period is None:
        print("No timesheet exists for the current period yet (first day). "
              "Re-run with --save to create it and enter time.")
        return
    print(f"Period {period.period_no}/{period.year} ({period.schedule}) ending "
          f"{period.end_dt}, status {period.status_cd}, {len(period.lines)} line(s)"
          f"{' [server today ' + ts.server_today + ']' if ts.server_today else ''}.")

    if period.day_index_for(target) is None:
        print(f"ERROR: {target} is not within the current period (ends {period.end_dt}). "
              "This script only operates on the current period.", file=sys.stderr)
        sys.exit(1)

    # Days to enter. The unattended daily run (no explicit --date) self-heals:
    # backfill every earlier weekday in this period that still has no time — so a
    # missed run (laptop asleep, network down) is recovered by the next one —
    # then enter today. An explicit --date touches only that one day.
    if args.date is None:
        missed = [d for d in period.weekday_dates()
                  if d < target and not period.hours_on(period.day_index_for(d))]
        if missed:
            print(f"Self-heal: backfilling {len(missed)} earlier weekday(s) with no "
                  f"time: {', '.join(f'{d:%b %d}' for d in missed)}.")
        days = missed + [target]
    else:
        days = [target]

    for day in days:
        period, _ = _enter_day(cp, ts, period, day, federal,
                               CHARGE_CHOICES[args.charge], args.hours, write, plan)

    # ── Sign: on the final working day of the period, audit then sign.
    final_day = period.final_working_day()
    is_final = final_day is not None and target == final_day
    if args.no_sign or not (is_final or args.sign_now):
        if is_final and args.no_sign:
            print("Final working day, but --no-sign set; not signing.")
        return

    # Re-load to get authoritative totals after this run's entry.
    period = ts.load_current_period(create_if_missing=False) or period
    expected = period.expected_hours(args.hours)
    total = period.total_entered()
    print(f"\nPeriod-end audit: {len(period.weekday_dates())} weekdays × "
          f"{hours_str(args.hours)}h = {hours_str(expected)}h expected; "
          f"{hours_str(total)}h entered.")
    if abs(total - expected) > 1e-6:
        print("Hours don't reconcile — NOT signing. Resolve the gap and re-run "
              "with --sign-now (or wait for the next scheduled run).", file=sys.stderr)
        sys.exit(1)
    if period.status_cd == "S" or period.status_cd == "P":
        print(f"Period already signed/processed (status {period.status_cd}); nothing to do.")
        return
    if _commit(cp, ts, period, ts.sign_requests(period),
               f"SIGN/submit period {period.period_no}/{period.year}", write):
        return
    sys.exit(1)


if __name__ == "__main__":
    main()
