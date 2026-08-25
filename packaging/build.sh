#!/usr/bin/env bash
#
# Build Costpoint Timesheet.app and wrap it in a .dmg — signed, notarized and
# stapled when the credentials are there, and plainly labelled as unsigned when
# they aren't. The GitHub release workflow runs this script and nothing else, so
# a release built here and a release built in CI are the same release.
#
#   packaging/build.sh                     # sign with the Developer ID it finds
#   packaging/build.sh --adhoc             # unsigned local build, no Apple account
#   packaging/build.sh --no-notarize       # sign, but don't wait on Apple
#
# Signing identity — one of, in order:
#   --identity "Developer ID Application: You (TEAMID)"
#   $SIGN_IDENTITY
#   the only "Developer ID Application" certificate in the keychain
#   otherwise: ad-hoc, and the .dmg is named accordingly
#
# Notarization credentials — an App Store Connect API key (preferred):
#   $NOTARY_KEY (path to the .p8), $NOTARY_KEY_ID, $NOTARY_ISSUER
# or an Apple ID and an app-specific password:
#   $NOTARY_APPLE_ID, $NOTARY_PASSWORD, $NOTARY_TEAM_ID
#
# See docs/RELEASING.md.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build"
DIST="$ROOT/dist"
ENTITLEMENTS="$ROOT/packaging/entitlements.plist"
APP_NAME="Costpoint Timesheet"
VOLUME_NAME="Costpoint Timesheet"

ARCH="${COSTPOINT_ARCH:-universal2}"
IDENTITY="${SIGN_IDENTITY:-}"
ADHOC=0
NOTARIZE=1

say()  { printf '\n\033[1m→ %s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --identity)    IDENTITY="$2"; shift 2 ;;
    --adhoc)       ADHOC=1; shift ;;
    --no-notarize) NOTARIZE=0; shift ;;
    --arch)        ARCH="$2"; shift 2 ;;
    -h|--help)     sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//;$d'; exit 0 ;;
    *)             die "unknown option: $1 (try --help)" ;;
  esac
done

[ "$(uname -s)" = "Darwin" ] || die "this builds a macOS app, and only macOS can build it"
command -v xcrun >/dev/null || die "the Xcode command line tools are missing: xcode-select --install"


# ── the interpreter ───────────────────────────────────────────────────────────
# It has to be a framework build, because that's the Python py2app copies into
# the bundle, and it has to be universal2 or the .dmg only runs on the machine
# that built it. The python.org installer is both; Homebrew's is neither.
say "Choosing an interpreter"
if [ -z "${PYTHON:-}" ]; then
  for candidate in \
      /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
      /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
      "$(command -v python3 || true)"; do
    if [ -x "$candidate" ]; then PYTHON="$candidate"; break; fi
  done
fi
[ -n "${PYTHON:-}" ] && [ -x "$PYTHON" ] || die "no usable python3 found; set \$PYTHON"

PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
FRAMEWORK_LIB="$("$PYTHON" -c 'import os, sys; print(os.path.join(sys.base_prefix, "Python"))')"
info "$PYTHON  (Python $PY_VERSION)"

[ -f "$FRAMEWORK_LIB" ] || die "$PYTHON is not a framework build, so py2app has no
  Python to put in the bundle. Install one from https://www.python.org/downloads/macos/
  and re-run, or point \$PYTHON at an existing framework build."

PY_ARCHS="$(lipo -archs "$FRAMEWORK_LIB" 2>/dev/null || echo unknown)"
info "framework: $FRAMEWORK_LIB  [$PY_ARCHS]"
if [ "$ARCH" = "universal2" ]; then
  case " $PY_ARCHS " in
    *" arm64 "*) case " $PY_ARCHS " in *" x86_64 "*) ;; *) ARCH="" ;; esac ;;
    *) ARCH="" ;;
  esac
  [ -n "$ARCH" ] || die "a universal2 build needs a universal2 interpreter, and this one
  is [$PY_ARCHS]. Install the universal2 build from python.org, or pass
  --arch $(uname -m) to build for this machine only."
fi
info "building for: $ARCH"

VERSION="$(cd "$ROOT" && "$PYTHON" -c 'import appversion; print(appversion.__version__)')"
info "version: $VERSION"


# ── signing identity ──────────────────────────────────────────────────────────
say "Choosing a signing identity"
if [ "$ADHOC" = 1 ]; then
  IDENTITY=""
  info "--adhoc: building an unsigned development app"
elif [ -z "$IDENTITY" ]; then
  FOUND="$(security find-identity -v -p codesigning 2>/dev/null \
           | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1 || true)"
  if [ -n "$FOUND" ]; then
    IDENTITY="$FOUND"
    info "found in the keychain: $IDENTITY"
  fi
fi

if [ -n "$IDENTITY" ]; then
  info "signing as: $IDENTITY"
  SIGN_ARGS=(--force --timestamp --options runtime --sign "$IDENTITY")
else
  # Ad-hoc still matters: an arm64 binary without any signature at all will not
  # load. It buys nothing with Gatekeeper, which is what the naming says.
  info "no Developer ID — signing ad-hoc. Gatekeeper will refuse this build"
  info "on any Mac but the one that built it (see README: 'Unsigned builds')."
  SIGN_ARGS=(--force --sign -)
  NOTARIZE=0
fi

have_notary_creds() {
  [ -n "${NOTARY_KEY:-}" ] && [ -n "${NOTARY_KEY_ID:-}" ] && [ -n "${NOTARY_ISSUER:-}" ] && return 0
  [ -n "${NOTARY_APPLE_ID:-}" ] && [ -n "${NOTARY_PASSWORD:-}" ] && [ -n "${NOTARY_TEAM_ID:-}" ] && return 0
  return 1
}
if [ "$NOTARIZE" = 1 ] && ! have_notary_creds; then
  info "no notarization credentials in the environment — skipping notarization"
  NOTARIZE=0
fi


# ── build ─────────────────────────────────────────────────────────────────────
say "Building the venv"
VENV="$BUILD/venv"
rm -rf "$DIST" "$BUILD"
mkdir -p "$BUILD"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet \
    -r "$ROOT/requirements.txt" -r "$ROOT/packaging/requirements-build.txt"
"$VENV/bin/python" -m pip freeze > "$BUILD/requirements-resolved.txt"
info "resolved: $(tr '\n' ' ' < "$BUILD/requirements-resolved.txt")"

say "Running py2app"
( cd "$ROOT" && COSTPOINT_ARCH="$ARCH" "$VENV/bin/python" setup.py py2app )

# Glob rather than assume: py2app names the bundle after the distribution, and
# a stray .app from an interrupted build would otherwise go unnoticed.
APP_COUNT=$(find "$DIST" -maxdepth 1 -name '*.app' | wc -l | tr -d ' ')
[ "$APP_COUNT" = 1 ] || die "expected exactly one .app in dist/, found $APP_COUNT"
APP="$(find "$DIST" -maxdepth 1 -name '*.app')"
[ "$(basename "$APP")" = "$APP_NAME.app" ] || die "py2app produced $(basename "$APP"), expected $APP_NAME.app"

MAIN_EXE="$APP/Contents/MacOS/$APP_NAME"
CLI_EXE="$APP/Contents/MacOS/costpoint-timesheet"
[ -x "$MAIN_EXE" ] || die "the menu bar executable is missing: $MAIN_EXE"
[ -x "$CLI_EXE" ]  || die "the CLI executable is missing: $CLI_EXE (check extra_scripts in setup.py)"
info "built $(basename "$APP")  [$(lipo -archs "$MAIN_EXE" 2>/dev/null || echo '?')]  $(du -sh "$APP" | cut -f1)"


# ── sign ──────────────────────────────────────────────────────────────────────
say "Signing"
# Innermost first — see packaging/signing_order.py. Loose Mach-O files can go in
# batches (none of them contains anything else); nested bundles go one at a time,
# in the order given, so each is sealed only after its contents are signed.
files=(); bundles=()
while IFS= read -r -d '' path; do
  if [ -d "$path" ]; then bundles+=("$path"); else files+=("$path"); fi
done < <("$PYTHON" "$ROOT/packaging/signing_order.py" "$APP")
info "${#files[@]} Mach-O files, ${#bundles[@]} nested bundles"

if [ "${#files[@]}" -gt 0 ]; then
  printf '%s\0' "${files[@]}" | xargs -0 -n 25 codesign "${SIGN_ARGS[@]}"
fi
for bundle in "${bundles[@]}"; do
  codesign "${SIGN_ARGS[@]}" "$bundle"
done

# Everything in Contents/MacOS needs the entitlements in its own right. Anything
# started from outside the app — launchd runs costpoint-timesheet directly — gets
# the hardened runtime with whatever its own signature grants and nothing else,
# and a bundled CPython without these exceptions dies on its first ctypes call.
# Not a list of names: py2app puts its own `python` in here alongside our two,
# and the next version of it may well put something else.
ENTITLE_ARGS=()
[ -n "$IDENTITY" ] && ENTITLE_ARGS=(--entitlements "$ENTITLEMENTS")
for executable in "$APP/Contents/MacOS"/*; do
  if [ -L "$executable" ] || [ ! -f "$executable" ]; then continue; fi
  info "entitling $(basename "$executable")"
  codesign "${SIGN_ARGS[@]}" "${ENTITLE_ARGS[@]+"${ENTITLE_ARGS[@]}"}" "$executable"
done

# Last, and with the entitlements: signing a bundle re-signs its CFBundleExecutable
# in place, so this is also what puts them on the menu bar app.
codesign "${SIGN_ARGS[@]}" "${ENTITLE_ARGS[@]+"${ENTITLE_ARGS[@]}"}" "$APP"

codesign --verify --strict --deep --verbose=1 "$APP"
info "signature verifies"


# ── does it actually work? ────────────────────────────────────────────────────
# Run the bundle we just signed, not the source tree: this is the last chance to
# catch a package py2app left out or a data file that ended up inside the zip,
# and the alternative to catching it here is catching it on someone else's Mac.
say "Checking the bundle"
"$CLI_EXE" --selftest || die "the bundle is incomplete — see the selftest output above"


# ── notarize the app, so first launch works offline ───────────────────────────
notarytool_args() {
  if [ -n "${NOTARY_KEY:-}" ]; then
    printf '%s\0' --key "$NOTARY_KEY" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER"
  else
    printf '%s\0' --apple-id "$NOTARY_APPLE_ID" --password "$NOTARY_PASSWORD" --team-id "$NOTARY_TEAM_ID"
  fi
}

notarize() {   # notarize <path-to-zip-dmg-or-pkg>
  local target="$1" args=()
  while IFS= read -r -d '' a; do args+=("$a"); done < <(notarytool_args)
  xcrun notarytool submit "$target" "${args[@]}" --wait --timeout 30m
  xcrun stapler staple "$target"
}

if [ "$NOTARIZE" = 1 ]; then
  say "Notarizing the app"
  # notarytool takes an archive, never a bundle. ditto keeps the signature and
  # the extended attributes that a plain zip would flatten.
  ditto -c -k --keepParent "$APP" "$BUILD/app.zip"
  notarize "$BUILD/app.zip"
  # Stapled into the .app itself, not just the .dmg: the ticket then travels
  # with the app once it's been dragged to /Applications, so the very first
  # launch doesn't have to reach Apple.
  xcrun stapler staple "$APP"
  info "ticket stapled to the app"
fi


# ── the disk image ────────────────────────────────────────────────────────────
say "Building the disk image"
if [ -n "$IDENTITY" ]; then
  DMG="$DIST/Costpoint-Timesheet-$VERSION.dmg"
else
  DMG="$DIST/Costpoint-Timesheet-$VERSION-unsigned.dmg"
fi

STAGE="$BUILD/dmg"
rm -rf "$STAGE"; mkdir -p "$STAGE"
ditto "$APP" "$STAGE/$(basename "$APP")"      # ditto, not cp: signatures survive
ln -s /Applications "$STAGE/Applications"     # the drag target, right there in the window

rm -f "$DMG"
hdiutil create -volname "$VOLUME_NAME" -srcfolder "$STAGE" -fs HFS+ \
               -format UDZO -imagekey zlib-level=9 -quiet -ov "$DMG"

if [ -n "$IDENTITY" ]; then
  codesign --force --timestamp --sign "$IDENTITY" "$DMG"
fi
if [ "$NOTARIZE" = 1 ]; then
  say "Notarizing the disk image"
  notarize "$DMG"
fi


# ── what came out ─────────────────────────────────────────────────────────────
say "Done"
info "app:  $APP"
info "dmg:  $DMG  ($(du -h "$DMG" | cut -f1))"
if [ "$NOTARIZE" = 1 ]; then
  info "notarized and stapled — this opens on any Mac with a double-click"
  spctl --assess --type open --context context:primary-signature -vv "$DMG" 2>&1 | sed 's/^/  /' || true
elif [ -n "$IDENTITY" ]; then
  info "signed but NOT notarized — Gatekeeper will still block it on other Macs"
else
  info "unsigned development build — see README, 'Unsigned builds'"
fi
