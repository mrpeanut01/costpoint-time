#!/usr/bin/env python3
"""
plan.py — shared state for the timesheet automation and the tray app.

Three small stores, all under ~/Library/Application Support/costpoint-timesheet
(NOT ~/Documents: macOS TCC blocks launchd agents there — see deploy.sh):

  .env          credentials, managed by the tray app's Credentials pane
  plan.json     the user's intent: which days are PTO, which holidays are observed
  status.json   cached "what Costpoint actually shows" — drives the red/green colours

plan.json is the contract between the tray app and the scheduled run: the tray
records intent, the daily run reads it and charges the right code on the day.
Everything here is stdlib-only (bar an optional `holidays` import) so timesheet.py
stays dependency-light.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta

APP_DIR = os.path.expanduser("~/Library/Application Support/costpoint-timesheet")
ENV_PATH = os.path.join(APP_DIR, ".env")
PLAN_PATH = os.path.join(APP_DIR, "plan.json")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STATUS_PATH = os.path.join(APP_DIR, "status.json")
LOG_PATH = os.path.expanduser("~/Library/Logs/costpoint-timesheet.log")

CRED_KEYS = ("COSTPOINT_ORGANIZATION", "COSTPOINT_USERNAME", "COSTPOINT_PASSWORD")


# ── .env handling ─────────────────────────────────────────────────────────────
def read_env_file(path: str) -> dict:
    """Parse a .env file. Only ' #' (whitespace-hash) starts an inline comment and
    surrounding quotes are honoured, so secrets containing '#' survive intact."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            key, val = ln.split("=", 1)
            val = val.strip()
            if " #" in val:
                val = val.split(" #", 1)[0].rstrip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            out[key.strip()] = val
    return out


def load_env() -> None:
    """Populate os.environ. Precedence: real environment > the tray-managed
    .env in APP_DIR > a .env in the working directory (dev convenience).

    setdefault gives first-writer-wins, so the order of these two calls IS the
    precedence: the tray app's copy is authoritative, a repo .env only fills gaps.
    """
    for path in (ENV_PATH, os.path.join(os.getcwd(), ".env")):
        for k, v in read_env_file(path).items():
            os.environ.setdefault(k, v)


def _atomic_write(path: str, text: str, mode: int = 0o600) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_credentials() -> dict:
    """{organization, username, password} from APP_DIR/.env (falling back to the
    live environment, so a shell-exported credential still shows up)."""
    env = read_env_file(ENV_PATH)
    return {
        "organization": env.get("COSTPOINT_ORGANIZATION") or os.environ.get("COSTPOINT_ORGANIZATION", ""),
        "username": env.get("COSTPOINT_USERNAME") or os.environ.get("COSTPOINT_USERNAME", ""),
        "password": env.get("COSTPOINT_PASSWORD") or os.environ.get("COSTPOINT_PASSWORD", ""),
    }


def save_credentials(organization: str, username: str, password: str) -> None:
    """Rewrite APP_DIR/.env with these credentials, preserving any other keys
    already in the file (DRY_RUN, HEADLESS, …). Written 0600, atomically."""
    existing = read_env_file(ENV_PATH)
    existing.update({
        "COSTPOINT_ORGANIZATION": organization.strip(),
        "COSTPOINT_USERNAME": username.strip(),
        "COSTPOINT_PASSWORD": password,
    })
    lines = ["# Managed by the Costpoint Timesheet tray app. Mode 0600.",
             "# Edit here or in the app's Credentials pane; the scheduled run reads this file.",
             ""]
    for key in CRED_KEYS:
        lines.append(f"{key}={existing.pop(key, '')}")
    extra = {k: v for k, v in existing.items() if v != ""}
    if extra:
        lines += ["", "# Other settings"]
        lines += [f"{k}={v}" for k, v in extra.items()]
    _atomic_write(ENV_PATH, "\n".join(lines) + "\n")
    # Make the new values visible to this process immediately.
    os.environ["COSTPOINT_ORGANIZATION"] = organization.strip()
    os.environ["COSTPOINT_USERNAME"] = username.strip()
    os.environ["COSTPOINT_PASSWORD"] = password


def have_credentials() -> bool:
    c = read_credentials()
    return all(c[k] for k in ("organization", "username", "password"))


# ── config.json ───────────────────────────────────────────────────────────────
class Config:
    """Everything that differs between one Costpoint tenant and the next.

    None of it is hardcoded: the server and organisation come from sign-in, and
    the charge codes are discovered from the account's own charge favourites —
    Costpoint flags which favourite is the holiday charge (HOLIDAY_FL) and which
    is the vacation/PTO charge (VACATION_FL), so only the working charge ever
    needs picking, and then only if the account has more than one.
    """
    VERSION = 1
    KINDS = ("work", "holiday", "pto")

    def __init__(self, data: dict | None = None):
        d = data or {}
        self.host: str = d.get("host") or os.environ.get("COSTPOINT_HOST", "")
        self.hours_per_day: float = float(d.get("hours_per_day") or 8)
        self.charges: dict[str, dict] = {k: dict(v) for k, v in (d.get("charges") or {}).items()}
        self.favorites: list[dict] = list(d.get("favorites") or [])

    @classmethod
    def load(cls) -> "Config":
        try:
            with open(CONFIG_PATH) as fh:
                return cls(json.load(fh))
        except (FileNotFoundError, ValueError, OSError):
            return cls()

    def save(self) -> None:
        _atomic_write(CONFIG_PATH, json.dumps({
            "version": self.VERSION,
            "host": self.host,
            "hours_per_day": self.hours_per_day,
            "charges": self.charges,
            "favorites": self.favorites,
        }, indent=2) + "\n", mode=0o644)

    # ── charges ───────────────────────────────────────────────────────────────
    def charge(self, kind: str) -> dict | None:
        c = self.charges.get(kind)
        return c if c and c.get("udt02") else None

    def label(self, kind: str) -> str:
        """The line description to match on, falling back to a readable name so
        messages stay sensible before setup has run."""
        c = self.charge(kind)
        return (c or {}).get("label") or {"work": "Work", "holiday": "Holiday",
                                          "pto": "PTO"}[kind]

    def set_charge(self, kind: str, udt02: str, label: str) -> None:
        self.charges[kind] = {"udt02": udt02, "label": label}

    def missing(self) -> list[str]:
        out = []
        if not self.host:
            out.append("server")
        out += [k for k in self.KINDS if not self.charge(k)]
        return out

    def is_complete(self) -> bool:
        return not self.missing()

    def apply_favorites(self, favorites: list[dict]) -> list[str]:
        """Store the account's favourites and auto-assign what we can. Returns a
        list of the kinds still needing a choice."""
        self.favorites = favorites
        by_kind = {"holiday": [f for f in favorites if f.get("holiday")],
                   "pto": [f for f in favorites if f.get("vacation")]}
        by_kind["work"] = [f for f in favorites
                           if not f.get("holiday") and not f.get("vacation")]
        for kind in self.KINDS:
            options = by_kind[kind]
            chosen = self.charge(kind)
            # Keep an existing choice while it's still a favourite; otherwise take
            # the obvious one, and only give up when the account offers several.
            if chosen and any(f["udt02"] == chosen["udt02"] for f in options):
                continue
            if len(options) == 1:
                self.set_charge(kind, options[0]["udt02"], options[0]["label"])
            elif not options:
                self.charges.pop(kind, None)
        return [k for k in self.KINDS if not self.charge(k)]


# ── holidays ──────────────────────────────────────────────────────────────────
def federal_holidays(*years: int):
    """US federal holidays for the given years; an empty dict if the `holidays`
    package isn't installed (the tray still runs, just without suggestions)."""
    try:
        import holidays as _h
    except ImportError:
        return {}
    if not years:
        years = (date.today().year,)
    return _h.US(years=sorted({yy for y in years for yy in (y, y + 1)}))


def _iso(d) -> str:
    return d.isoformat() if isinstance(d, date) else str(d)


# ── plan.json ─────────────────────────────────────────────────────────────────
class Plan:
    """The user's intent, as set in the tray app's calendar.

    pto       — dates to charge PTO instead of the working charge.
    holidays  — explicit overrides on top of the federal calendar:
                  True  = observe this day as a holiday (company holiday)
                  False = work this day even though it's a federal holiday
                A federal holiday with no entry is *unconfirmed*: still treated as
                a holiday (that's the safe default and the previous behaviour),
                but the calendar flags it so it can be confirmed or declined.
    """
    VERSION = 1

    def __init__(self, data: dict | None = None):
        data = data or {}
        self.pto: set[str] = set(data.get("pto", []))
        self.holidays: dict[str, bool] = {k: bool(v) for k, v in (data.get("holidays") or {}).items()}

    @classmethod
    def load(cls) -> "Plan":
        try:
            with open(PLAN_PATH) as fh:
                return cls(json.load(fh))
        except (FileNotFoundError, ValueError, OSError):
            return cls()

    def save(self) -> None:
        _atomic_write(PLAN_PATH, json.dumps({
            "version": self.VERSION,
            "pto": sorted(self.pto),
            "holidays": dict(sorted(self.holidays.items())),
        }, indent=2) + "\n", mode=0o644)

    # ── queries ───────────────────────────────────────────────────────────────
    def is_pto(self, d) -> bool:
        return _iso(d) in self.pto

    def is_holiday(self, d, federal=None) -> bool:
        iso = _iso(d)
        if iso in self.pto:                  # PTO beats the holiday calendar
            return False
        if iso in self.holidays:
            return self.holidays[iso]
        if federal is None:
            return False
        dd = d if isinstance(d, date) else datetime.strptime(iso, "%Y-%m-%d").date()
        return dd in federal

    def holiday_unconfirmed(self, d, federal=None) -> bool:
        """A federal holiday the user hasn't yet confirmed or declined."""
        iso = _iso(d)
        if iso in self.pto or iso in self.holidays or federal is None:
            return False
        dd = d if isinstance(d, date) else datetime.strptime(iso, "%Y-%m-%d").date()
        return dd in federal

    def holiday_name(self, d, federal=None) -> str:
        if federal is None:
            return "Company holiday"
        dd = d if isinstance(d, date) else datetime.strptime(_iso(d), "%Y-%m-%d").date()
        return federal.get(dd) or "Company holiday"

    # ── mutations ─────────────────────────────────────────────────────────────
    def set_pto(self, d, on: bool) -> None:
        iso = _iso(d)
        if on:
            self.pto.add(iso)
            self.holidays.pop(iso, None)     # a day is PTO or a holiday, not both
        else:
            self.pto.discard(iso)

    def seed_federal(self, *years: int) -> int:
        """Pre-fill the US federal holidays for `years` as observed, so they show
        up already marked instead of asking to be confirmed one at a time.

        Idempotent and non-destructive: a day the user has explicitly set (either
        way) is left alone, so declining "we work that day" sticks. Weekend dates
        are skipped — the `holidays` package lists both the real date and the
        observed weekday for e.g. July 4 on a Saturday, and only the observed one
        is a working day.
        """
        federal = federal_holidays(*years)
        added = 0
        for d in federal:
            iso = d.isoformat()
            if d.weekday() >= 5 or iso in self.holidays:
                continue
            self.holidays[iso] = True
            added += 1
        return added

    def set_holiday(self, d, observe) -> None:
        """observe True = holiday, False = normal workday, None = back to default."""
        iso = _iso(d)
        if observe is None:
            self.holidays.pop(iso, None)
        else:
            self.holidays[iso] = bool(observe)
            if observe:
                self.pto.discard(iso)


    def set_state(self, d, target: str, federal=None) -> None:
        """Put a day into one of the three states the menu bar strip cycles
        through: 'normal', 'holiday', 'pto'.

        'normal' on a federal holiday has to be recorded as an explicit decline
        (False), not as "no opinion" — otherwise the federal calendar would put
        the holiday straight back.
        """
        iso = _iso(d)
        dd = d if isinstance(d, date) else datetime.strptime(iso, "%Y-%m-%d").date()
        is_federal = bool(federal) and dd in federal
        self.pto.discard(iso)
        if target == "pto":
            self.pto.add(iso)
            self.holidays[iso] = False if is_federal else None
            if self.holidays[iso] is None:
                self.holidays.pop(iso)
        elif target == "holiday":
            self.holidays[iso] = True
        else:                                            # normal working day
            if is_federal:
                self.holidays[iso] = False
            else:
                self.holidays.pop(iso, None)


# ── the scheduled launchd job ─────────────────────────────────────────────────
JOB_LABEL = "com.costpoint-timesheet.daily"
JOB_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{JOB_LABEL}.plist")


def _job() -> tuple[str, str]:
    return JOB_LABEL, JOB_PLIST


def read_schedule() -> tuple[int, int] | None:
    """(hour, minute) the daily job is set to fire at, or None if it isn't
    installed yet (i.e. deploy.sh hasn't been run)."""
    import plistlib
    try:
        with open(_job()[1], "rb") as fh:
            pl = plistlib.load(fh)
    except (OSError, ValueError):
        return None
    cal = pl.get("StartCalendarInterval") or []
    if isinstance(cal, dict):
        cal = [cal]
    if not cal:
        return None
    return int(cal[0].get("Hour", 9)), int(cal[0].get("Minute", 0))


def write_schedule(hour: int, minute: int) -> str:
    """Repoint the daily job at a new time and reload it. Returns a status line.

    Rewrites only StartCalendarInterval, so everything else deploy.sh put in the
    plist (paths, logging, environment) is preserved.
    """
    import plistlib
    import subprocess
    label, path = _job()
    if not os.path.exists(path):
        raise FileNotFoundError("The daily job isn't installed yet — run ./deploy.sh first.")
    with open(path, "rb") as fh:
        pl = plistlib.load(fh)
    pl["StartCalendarInterval"] = [
        {"Weekday": wd, "Hour": int(hour), "Minute": int(minute)} for wd in range(1, 6)
    ]
    with open(path, "wb") as fh:
        plistlib.dump(pl, fh)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                   capture_output=True, check=False)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.returncode}")
    return f"Daily run moved to {fmt_time(hour, minute)}."


def next_run(hour: int, minute: int, now: datetime | None = None) -> datetime:
    """Next weekday occurrence of hour:minute (the job runs Mon–Fri)."""
    now = now or datetime.now()
    cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while cand <= now or cand.weekday() >= 5:
        cand = (cand + timedelta(days=1)).replace(hour=hour, minute=minute,
                                                  second=0, microsecond=0)
    return cand


def fmt_time(hour: int, minute: int) -> str:
    ampm = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {ampm}"


# ── status.json ───────────────────────────────────────────────────────────────
class Status:
    """Cached view of what Costpoint shows, keyed by ISO date.

    days[iso] = {"hours": 8.0, "label": "Capture & Proposal Dev.", "signed": bool}
    A date present with hours > 0 is green; a weekday inside a loaded period with
    no hours is red. Dates never loaded are simply unknown (grey) — the cache
    accumulates as periods are synced, so history stays visible offline.
    """
    def __init__(self, data: dict | None = None):
        data = data or {}
        self.days: dict[str, dict] = data.get("days", {})
        self.periods: dict[str, dict] = data.get("periods", {})   # key -> {start,end,status,signed}
        self.updated: str | None = data.get("updated")
        self.error: str | None = data.get("error")

    @classmethod
    def load(cls) -> "Status":
        try:
            with open(STATUS_PATH) as fh:
                return cls(json.load(fh))
        except (FileNotFoundError, ValueError, OSError):
            return cls()

    def save(self) -> None:
        _atomic_write(STATUS_PATH, json.dumps({
            "updated": self.updated,
            "error": self.error,
            "days": dict(sorted(self.days.items())),
            "periods": dict(sorted(self.periods.items())),
        }, indent=2) + "\n", mode=0o644)

    def covered(self, d) -> bool:
        """True if `d` fell inside a period we've actually loaded — the difference
        between 'no time entered' (red) and 'we don't know' (grey)."""
        iso = _iso(d)
        return any(p["start"] <= iso <= p["end"] for p in self.periods.values())

    def signed(self, d) -> bool:
        iso = _iso(d)
        return any(p["start"] <= iso <= p["end"] and p.get("signed")
                   for p in self.periods.values())


# ── the merged per-day view the UI and the menu both render ───────────────────
#   filled    green    ordinary working hours are in Costpoint
#   missing   red      a weekday that should have time and doesn't
#   pto       purple   PTO — solid once entered, tinted while only planned
#   holiday   amber    holiday — solid once entered, tinted while only planned
#   unsure    amber?   federal holiday awaiting confirmation
#   future    grey     a workday still ahead of us
#   weekend   blank
#   unknown   blank    outside any period we've loaded
def entered_kind(label: str, udt02: str, config: "Config | None") -> str:
    """Classify a day's entered charge as 'pto' | 'holiday' | 'normal'.

    Matching the configured charge code is exact; the description is only a
    fallback for a line entered before setup ran, or added straight in Costpoint.
    """
    if config and udt02:
        for kind in Config.KINDS:
            c = config.charge(kind)
            if c and c["udt02"] == udt02:
                return {"work": "normal"}.get(kind, kind)
    up = (label or "").upper()
    if "PTO" in up or "VACATION" in up:
        return "pto"
    if "HOLIDAY" in up:
        return "holiday"
    return "normal"


def day_view(d: date, plan: Plan, status: Status, federal=None, today: date | None = None,
             pending: dict | None = None, config: "Config | None" = None) -> dict:
    today = today or date.today()
    iso = d.isoformat()
    entry = status.days.get(iso) or {}
    hours = float(entry.get("hours") or 0)
    view = {
        "date": iso,
        "hours": hours,
        "label": entry.get("label", ""),
        "pto": plan.is_pto(d),
        "holiday": plan.is_holiday(d, federal),
        "unconfirmed": plan.holiday_unconfirmed(d, federal),
        "holiday_name": "",
        "signed": status.signed(d),
        "weekend": d.weekday() >= 5,
        "today": d == today,
    }
    if view["holiday"] or view["unconfirmed"]:
        view["holiday_name"] = plan.holiday_name(d, federal)

    # A change the user just made that hasn't reached Costpoint yet. Held
    # explicitly by the app rather than inferred from plan-vs-timesheet
    # disagreement — a day entered as PTO straight in Costpoint, which the plan
    # knows nothing about, is settled fact, not a pending change.
    want = (pending or {}).get(iso)
    entered = entered_kind(entry.get("label", ""), entry.get("udt02", ""), config) \
        if hours > 0 else None
    view["pending"] = want
    view["entered"] = False

    if d.weekday() >= 5:
        view["state"] = "weekend"
    elif want is not None:
        # Show the intent right away, outlined, so a click never looks inert.
        view["state"] = {"pto": "pto", "holiday": "holiday",
                         "normal": "future" if d > today else "missing"}[want]
    elif entered is not None:
        view["entered"] = True
        view["state"] = {"pto": "pto", "normal": "filled",
                         "holiday": "unsure" if view["unconfirmed"] else "holiday"}[entered]
    elif view["pto"]:
        view["state"] = "pto"
    elif view["unconfirmed"]:
        view["state"] = "unsure"
    elif view["holiday"]:
        view["state"] = "holiday"
    elif d > today:
        view["state"] = "future"
    elif status.covered(d):
        view["state"] = "missing"
    else:
        view["state"] = "unknown"
    return view
