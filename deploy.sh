#!/bin/zsh
# Install the timesheet automation from this source checkout, for development.
#
# Most people want the .dmg instead — https://github.com/mrpeanut01/costpoint-time/releases
# — which is the same code in an app bundle with its own Python inside. This
# script is for running the code you're editing.
#
# Why it deploys OUT of the repo: macOS TCC blocks launchd background agents
# from reading ~/Documents (there's no GUI session to grant consent), so a venv
# living there hangs forever in open() at Python startup when launchd starts it,
# while working perfectly from Terminal. ~/Library/Application Support isn't
# TCC-protected, so running from there avoids it entirely.
#
# Installs the same two launchd jobs the .app installs, via the same code:
#   com.costpoint-timesheet.daily   weekdays, fills and signs the timesheet
#   com.costpoint-timesheet.tray    at login, the menu bar app
#
# Re-run it any time you change the code — it restarts the tray. Credentials are
# NOT overwritten: the tray app owns .env in the deploy dir, and this script only
# seeds it the first time (from the repo's .env, if you have one).
#
# Usage: ./deploy.sh

set -euo pipefail

SRC="${0:A:h}"                                   # this repo (wherever it lives)
DEPLOY="$HOME/Library/Application Support/costpoint-timesheet"
LOG="$HOME/Library/Logs/costpoint-timesheet.log"
LABEL="com.costpoint-timesheet.daily"
TRAY_LABEL="com.costpoint-timesheet.tray"

PYTHON="/opt/homebrew/opt/python@3.13/bin/python3.13"   # stable Homebrew path

echo "→ Deploy target: $DEPLOY"
mkdir -p "$DEPLOY" "$HOME/Library/Logs" "$HOME/Library/LaunchAgents"

# Retire any agent from an earlier install that points at this deploy directory
# but isn't one of the two we're about to write — otherwise a renamed label
# leaves a second copy of the job running against stale code.
for pl in "$HOME/Library/LaunchAgents"/*.plist(N); do
  lbl=$(/usr/libexec/PlistBuddy -c "Print :Label" "$pl" 2>/dev/null) || continue
  [[ "$lbl" == "$LABEL" || "$lbl" == "$TRAY_LABEL" ]] && continue
  if grep -q "costpoint-timesheet" "$pl" 2>/dev/null; then
    echo "→ Removing superseded agent ${lbl}"
    launchctl bootout "gui/$(id -u)/${lbl}" 2>/dev/null || true
    rm -f "$pl"
  fi
done

# Credentials: the tray app is the owner of $DEPLOY/.env. Seed it once from the
# repo if it isn't there yet; never clobber it afterwards.
if [[ ! -f "$DEPLOY/.env" ]]; then
  if [[ -f "$SRC/.env" ]]; then
    echo "→ Seeding credentials from $SRC/.env (first deploy only)"
    cp "$SRC/.env" "$DEPLOY/.env"
  else
    echo "→ No credentials yet — set them in the tray app's Credentials pane."
    : > "$DEPLOY/.env"
  fi
fi
chmod 600 "$DEPLOY/.env"

echo "→ Copying code"
cp "$SRC/timesheet.py" "$SRC/costpoint_mobile.py" "$SRC/plan.py" \
   "$SRC/tray.py" "$SRC/appversion.py" "$SRC/requirements.txt" "$DEPLOY/"

echo "→ Building self-contained venv"
if [[ ! -x "$DEPLOY/.venv/bin/python3" ]]; then
  "$PYTHON" -m venv "$DEPLOY/.venv"
fi
"$DEPLOY/.venv/bin/python3" -m pip install -q --upgrade pip
"$DEPLOY/.venv/bin/python3" -m pip install -q -r "$DEPLOY/requirements.txt"

echo "→ Writing executable launcher"
cat > "$DEPLOY/costpoint-timesheet" <<'LAUNCHER'
#!/bin/zsh
# Self-contained launcher. Pass any timesheet.py args, e.g. --save, --sign-now.
set -euo pipefail
HERE="${0:A:h}"
cd "$HERE"
exec "$HERE/.venv/bin/python3" "$HERE/timesheet.py" "$@"
LAUNCHER
chmod +x "$DEPLOY/costpoint-timesheet"

# The plists are written by plan.py, which is also what the .app uses and what
# the tray's "run weekdays at" menu rewrites. Running it from $DEPLOY with the
# venv's interpreter is what makes the jobs point at this deployed copy, and
# force=True restarts them even when the plists themselves haven't changed —
# the code behind them just did.
echo "→ Installing launch agents"
( cd "$DEPLOY" && "$DEPLOY/.venv/bin/python3" -c '
import plan
schedule = plan.read_schedule() or (plan.DEFAULT_HOUR, plan.DEFAULT_MINUTE)
plan.sync_agents(force=True)
print(f"✓ {plan.DAILY_LABEL} — weekdays at {plan.fmt_time(*schedule)}")
print(f"✓ {plan.TRAY_LABEL} — at login")
' )

echo
echo "Done. Deployed to: $DEPLOY"
echo "Log:        $LOG"
echo "The menu bar icon should be up now — click it for status, PTO and the calendar."
echo
echo "Run now:    \"$DEPLOY/costpoint-timesheet\" --save        # real write"
echo "Dry run:    \"$DEPLOY/costpoint-timesheet\"               # no write"
echo "Run daily:  launchctl kickstart -k gui/$(id -u)/${LABEL}"
echo "Tray off:   launchctl bootout   gui/$(id -u)/${TRAY_LABEL}"
