#!/bin/zsh
# Deploy the timesheet automation as a self-contained app OUTSIDE ~/Documents.
#
# Why this exists: macOS TCC blocks launchd background agents from reading
# ~/Documents (no GUI session to grant consent), so a venv/script living there
# hangs forever in open() at Python startup when run from launchd. Running from
# ~/Library/Application Support (not TCC-protected) avoids it entirely.
#
# Installs two launchd jobs:
#   com.costpoint-timesheet.daily   weekdays, fills and signs the timesheet
#   com.costpoint-timesheet.tray    at login, the menu bar app
#
# Re-run it any time you change the code. Credentials are NOT overwritten: the
# tray app owns .env in the deploy dir, and this script only seeds it the first
# time (from the repo's .env, if you have one).
#
# Usage: ./deploy.sh

set -euo pipefail

SRC="${0:A:h}"                                   # this repo (wherever it lives)
DEPLOY="$HOME/Library/Application Support/costpoint-timesheet"
LOG="$HOME/Library/Logs/costpoint-timesheet.log"
LABEL="com.costpoint-timesheet.daily"
TRAY_LABEL="com.costpoint-timesheet.tray"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
TRAY_PLIST="$HOME/Library/LaunchAgents/${TRAY_LABEL}.plist"

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
   "$SRC/tray.py" "$SRC/requirements.txt" "$DEPLOY/"

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

echo "→ Installing launchd plist (weekdays 09:00 America/New_York)"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <!-- Call the venv Python directly. App lives in ~/Library/Application Support,
       which (unlike ~/Documents) is NOT TCC-protected, so launchd can read it. -->
  <key>ProgramArguments</key>
  <array>
    <string>${DEPLOY}/.venv/bin/python3</string>
    <string>${DEPLOY}/timesheet.py</string>
    <string>--save</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${DEPLOY}</string>

  <!-- 09:00 local. Mac is on America/New_York, so this is 9 AM Eastern. Running
       early means a missed prior day is self-healed before it counts as late. -->
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  </array>

  <key>StandardOutPath</key>
  <string>${LOG}</string>
  <key>StandardErrorPath</key>
  <string>${LOG}</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>

  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
PLIST

echo "→ Installing tray plist (menu bar app, starts at login)"
cat > "$TRAY_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${TRAY_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${DEPLOY}/.venv/bin/python3</string>
    <string>${DEPLOY}/tray.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${DEPLOY}</string>

  <key>RunAtLoad</key>
  <true/>

  <!-- Restart if it crashes, but respect Quit from the menu (a clean exit). -->
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>

  <key>StandardOutPath</key>
  <string>${LOG}</string>
  <key>StandardErrorPath</key>
  <string>${LOG}</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
</dict>
</plist>
PLIST

echo "→ Reloading launchd jobs"
for L in "${LABEL}:${PLIST}" "${TRAY_LABEL}:${TRAY_PLIST}"; do
  lbl="${L%%:*}"; pl="${L#*:}"
  launchctl bootout "gui/$(id -u)/${lbl}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$pl"
  launchctl print "gui/$(id -u)/${lbl}" >/dev/null && echo "✓ Loaded $lbl"
done

echo
echo "Done. Deployed to: $DEPLOY"
echo "Log:        $LOG"
echo "The menu bar icon should be up now — click it for status, PTO and the calendar."
echo
echo "Run now:    \"$DEPLOY/costpoint-timesheet\" --save        # real write"
echo "Dry run:    \"$DEPLOY/costpoint-timesheet\"               # no write"
echo "Run daily:  launchctl kickstart -k gui/$(id -u)/${LABEL}"
echo "Tray off:   launchctl bootout   gui/$(id -u)/${TRAY_LABEL}"
