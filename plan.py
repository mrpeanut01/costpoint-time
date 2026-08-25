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
import sys
import tempfile
from datetime import date, datetime, timedelta

APP_DIR = os.path.expanduser("~/Library/Application Support/costpoint-timesheet")
ENV_PATH = os.path.join(APP_DIR, ".env")
PLAN_PATH = os.path.join(APP_DIR, "plan.json")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STATUS_PATH = os.path.join(APP_DIR, "status.json")
LOG_PATH = os.path.expanduser("~/Library/Logs/costpoint-timesheet.log")

CRED_KEYS = ("COSTPOINT_ORGANIZATION", "COSTPOINT_USERNAME", "COSTPOINT_PASSWORD")

# The log is append-only and shared by both launch agents, so it can only be
# trimmed in a way that survives another process holding an inherited fd on it.
LOG_MAX_BYTES = 4 * 1024 * 1024


# ── the log ───────────────────────────────────────────────────────────────────
def _stdout_is_captured() -> bool:
    """Is anything actually listening to stdout?

    launchd points the agents' stdout at LOG_PATH, and a terminal is obviously
    live — but an app double-clicked in Finder gets /dev/null, and everything
    printed there is lost.
    """
    import stat
    try:
        fd = sys.stdout.fileno()
        st = os.fstat(fd)
    except (AttributeError, ValueError, OSError):
        return False
    if stat.S_ISCHR(st.st_mode):          # a tty counts; /dev/null does not
        return os.isatty(fd)
    return True                            # a file, pipe or socket: someone has it


def setup_logging() -> None:
    """Make sure this process's output reaches ~/Library/Logs, however it started."""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            # Truncate in place rather than rotate: launchd and any already-running
            # sibling hold O_APPEND fds on this exact inode, and renaming the file
            # would send their writes to something nobody can read.
            os.truncate(LOG_PATH, 0)
    except OSError:
        pass
    if _stdout_is_captured():
        return
    try:
        fh = open(LOG_PATH, "a", buffering=1)
    except OSError:
        return
    sys.stdout = sys.stderr = fh


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


# ── which copy of the app is this? ────────────────────────────────────────────
# Everything below has to name an executable in a launchd plist, and that
# executable lives somewhere different depending on how the app was installed:
# inside Costpoint Timesheet.app when it came from the .dmg, or next to this
# file when it was deployed from a source checkout by deploy.sh.
BUNDLE_ID = "com.costpoint-timesheet.app"
CLI_EXECUTABLE = "costpoint-timesheet"      # the bundle's command-line entry point


def is_bundled() -> bool:
    """True when running from inside a py2app .app bundle."""
    return getattr(sys, "frozen", "") == "macosx_app"


def app_bundle() -> str | None:
    """The enclosing .app, or None when running from a source checkout."""
    if not is_bundled():
        return None
    path = os.path.abspath(sys.executable)
    while path not in ("/", ""):
        if path.endswith(".app") and os.path.exists(os.path.join(path, "Contents", "Info.plist")):
            return path
        path = os.path.dirname(path)
    return None


def bundle_version() -> str:
    """The bundle's CFBundleShortVersionString, or the source version."""
    bundle = app_bundle()
    if bundle:
        import plistlib
        try:
            with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as fh:
                v = plistlib.load(fh).get("CFBundleShortVersionString")
            if v:
                return str(v)
        except (OSError, ValueError):
            pass
    try:
        from appversion import __version__
        return __version__
    except ImportError:
        return "dev"


QUARANTINE_FIX = 'xattr -dr com.apple.quarantine "/Applications/Costpoint Timesheet.app"'


def bundle_is_unstable() -> str:
    """Why this copy is in no fit state to be wired into launchd — '' if it is.

    An app run straight off the .dmg lives under /Volumes and vanishes when the
    image is ejected; an app still carrying the quarantine flag gets *translocated*
    by Gatekeeper and runs from a randomly-named read-only mount that won't exist
    next time. Pointing a launch agent at either produces a job that silently
    never runs again.

    One line, because this also reaches the menu as a status line and travels as
    an exception message. bundle_fix_advice() is the long version.
    """
    bundle = app_bundle()
    if not bundle:
        return ""
    if "/AppTranslocation/" in bundle:
        return ("macOS is running Costpoint Timesheet from a temporary copy of its own, "
                "because the app is still flagged as downloaded.")
    if bundle.startswith("/Volumes/"):
        return "Costpoint Timesheet is running from a disk image, not from your Mac."
    return ""


def bundle_fix_advice() -> str:
    """What to do about it, for somewhere with room to say it.

    Worth distinguishing the two: telling someone to move the app to Applications
    when it is already sitting in Applications, and the real problem is a flag
    macOS set on it at download, is a dead end.
    """
    if "/AppTranslocation/" in (app_bundle() or ""):
        return ("The app itself is fine — this is the flag macOS puts on anything "
                "downloaded, and it stays set because these builds aren't signed with "
                "an Apple Developer ID.\n\n"
                "Move the app to your Applications folder if it isn't there already, "
                "then run this once in Terminal:\n\n"
                f"    {QUARANTINE_FIX}\n\n"
                "and open the app again.")
    return ("Drag the app into your Applications folder, then clear the flag macOS "
            "put on it when it was downloaded:\n\n"
            f"    {QUARANTINE_FIX}\n\n"
            "and open it from Applications.")


def _executable(name: str, fallback_script: str) -> list[str]:
    """argv for one of our two entry points, bundled or not."""
    bundle = app_bundle()
    if bundle:
        return [os.path.join(bundle, "Contents", "MacOS", name)]
    here = os.path.dirname(os.path.abspath(__file__))
    return [sys.executable, os.path.join(here, fallback_script)]


def _main_executable_name() -> str:
    """CFBundleExecutable — the GUI stub's filename inside Contents/MacOS."""
    bundle = app_bundle()
    if not bundle:
        return ""
    import plistlib
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as fh:
            return str(plistlib.load(fh).get("CFBundleExecutable") or "")
    except (OSError, ValueError):
        return ""


def tray_command() -> list[str]:
    """argv that starts the menu bar app. `--agent` tells it launchd is the
    caller, which changes how it behaves when another copy already holds the
    single-instance lock."""
    return _executable(_main_executable_name(), "tray.py") + ["--agent"]


def daily_command() -> list[str]:
    """argv for the scheduled run that actually files the timesheet."""
    return _executable(CLI_EXECUTABLE, "timesheet.py") + ["--save"]


# ── the launch agents ─────────────────────────────────────────────────────────
#   com.costpoint-timesheet.daily   weekdays at the hour you pick: file and sign
#   com.costpoint-timesheet.tray    at login: the menu bar app
#
# Both plists are generated here rather than by an install script, so the app
# can repair them itself — dragging Costpoint Timesheet.app to a new folder
# would otherwise leave two launchd jobs pointing at a path that no longer
# exists, and nothing would say so until the day a timesheet went unfiled.
DAILY_LABEL = "com.costpoint-timesheet.daily"
TRAY_LABEL = "com.costpoint-timesheet.tray"
LAUNCH_AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
DAILY_PLIST = os.path.join(LAUNCH_AGENTS_DIR, f"{DAILY_LABEL}.plist")
TRAY_PLIST = os.path.join(LAUNCH_AGENTS_DIR, f"{TRAY_LABEL}.plist")

DEFAULT_HOUR, DEFAULT_MINUTE = 9, 0
WEEKDAYS = range(1, 6)                       # launchd: Monday…Friday
TRAY_RETRY_HOUR = 7                          # see tray_plist() for why

# Kept because deploy.sh and older checkouts import them by these names.
JOB_LABEL = DAILY_LABEL
JOB_PLIST = DAILY_PLIST


def daily_plist(hour: int, minute: int) -> dict:
    """Run early: a day missed while the Mac was asleep is backfilled before it
    counts as late."""
    return {
        "Label": DAILY_LABEL,
        "ProgramArguments": daily_command(),
        "WorkingDirectory": APP_DIR,
        "StartCalendarInterval": [
            {"Weekday": wd, "Hour": int(hour), "Minute": int(minute)} for wd in WEEKDAYS
        ],
        "StandardOutPath": LOG_PATH,
        "StandardErrorPath": LOG_PATH,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "RunAtLoad": False,
    }


def tray_plist() -> dict:
    return {
        "Label": TRAY_LABEL,
        "ProgramArguments": tray_command(),
        "WorkingDirectory": APP_DIR,
        "RunAtLoad": True,
        # Restart it if it crashes, but respect Quit from the menu (a clean exit).
        "KeepAlive": {"SuccessfulExit": False},
        # RunAtLoad only fires at login, so on a Mac that stays logged in for
        # weeks a clean Quit would leave the app down indefinitely. Try again
        # each weekday morning; the app's own lock file means a second start
        # while it's already running is a no-op.
        "StartCalendarInterval": [
            {"Weekday": wd, "Hour": TRAY_RETRY_HOUR, "Minute": 0} for wd in WEEKDAYS
        ],
        "StandardOutPath": LOG_PATH,
        "StandardErrorPath": LOG_PATH,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def _read_plist(path: str) -> dict | None:
    import plistlib
    try:
        with open(path, "rb") as fh:
            return plistlib.load(fh)
    except (OSError, ValueError):
        return None


def _write_plist(path: str, data: dict) -> None:
    import plistlib
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".plist")
    try:
        with os.fdopen(fd, "wb") as fh:
            plistlib.dump(data, fh)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _reload_agent(label: str, path: str) -> None:
    import subprocess
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                   capture_output=True, check=False)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.returncode}")


def sync_agents(managed: bool = False, force: bool = False) -> list[str]:
    """Point both launch agents at *this* copy of the app. Returns the labels
    that actually needed rewriting, so a normal launch is silent.

    `managed=True` means launchd started this very process from the tray agent.
    Its plist is then written but not reloaded: booting the job out would kill
    us mid-launch, and launchd re-reads ~/Library/LaunchAgents at every login,
    so the new definition takes effect on its own.

    `force=True` reloads both jobs even when their plists already say the right
    thing. That is what deploy.sh wants — the code behind an unchanged plist has
    just been replaced, and a running tray would otherwise carry on as it was.
    """
    reason = bundle_is_unstable()
    if reason:
        raise RuntimeError(reason)

    hour, minute = read_schedule() or (DEFAULT_HOUR, DEFAULT_MINUTE)
    changed = []
    for path, want, reload_it in (
        (DAILY_PLIST, daily_plist(hour, minute), True),
        (TRAY_PLIST, tray_plist(), not managed),
    ):
        stale = _read_plist(path) != want
        if not stale and not force:
            continue
        if stale:
            _write_plist(path, want)
            changed.append(want["Label"])
        if reload_it:
            _reload_agent(want["Label"], path)
    return changed


def remove_agents() -> None:
    """Unload and delete both agents. The app's data in APP_DIR is left alone."""
    import subprocess
    uid = os.getuid()
    for label, path in ((DAILY_LABEL, DAILY_PLIST), (TRAY_LABEL, TRAY_PLIST)):
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                       capture_output=True, check=False)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def read_schedule() -> tuple[int, int] | None:
    """(hour, minute) the daily job fires at, or None if it isn't installed."""
    cal = (_read_plist(DAILY_PLIST) or {}).get("StartCalendarInterval") or []
    if isinstance(cal, dict):
        cal = [cal]
    if not cal:
        return None
    return int(cal[0].get("Hour", DEFAULT_HOUR)), int(cal[0].get("Minute", 0))


def write_schedule(hour: int, minute: int) -> str:
    """Move the daily job to a new time and reload it. Returns a status line.

    The plist is regenerated rather than patched, so this doubles as a repair:
    a job left pointing at an app that has since moved is fixed by picking a
    time — or by any launch of the app, via sync_agents().
    """
    reason = bundle_is_unstable()
    if reason:
        raise RuntimeError(reason)
    _write_plist(DAILY_PLIST, daily_plist(hour, minute))
    _reload_agent(DAILY_LABEL, DAILY_PLIST)
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
