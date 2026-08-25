#!/usr/bin/env python3
"""costpoint-timesheet — the command line entry point inside the .app bundle.

py2app builds every `extra_scripts` entry into its own executable in
Contents/MacOS, named after this file (see setup.py). That executable is what
the daily launch agent runs, and it is a perfectly ordinary CLI:

    "/Applications/Costpoint Timesheet.app/Contents/MacOS/costpoint-timesheet" --save

The arguments are timesheet.py's — this file exists only so the bundle has a
headless entry point that doesn't drag AppKit and the menu bar in with it.
There is one extra flag, --selftest, described below.
"""
import sys

import plan as planning


def selftest() -> int:
    """Prove the bundle is whole: every import resolves and TLS verifies.

    Packaging failures are quiet ones — a package py2app left out, a data file
    that ended up inside the zip where nothing can open it — and they surface as
    a menu bar icon that never appears, on someone else's Mac, a week later.
    build.sh runs this against the signed bundle before the .dmg is made, and
    it's a fair first thing to ask for in a bug report.
    """
    import platform
    import ssl
    import urllib.request

    ok = True
    print(f"Costpoint Timesheet {planning.bundle_version()}")
    print(f"  bundle      {planning.app_bundle() or '(running from source)'}")
    print(f"  python      {platform.python_version()} ({platform.machine()})")

    for name in ("rumps", "AppKit", "Foundation", "objc", "holidays", "certifi",
                 "timesheet", "costpoint_mobile"):
        try:
            module = __import__(name)
            where = getattr(module, "__file__", "built-in")
            print(f"  import      {name:<16} {where}")
        except Exception as e:                      # noqa: BLE001 — report, don't raise
            print(f"  IMPORT FAIL {name:<16} {e}")
            ok = False

    # The one that packaging most often breaks: a CA bundle the app can't reach.
    # Showing the interpreter's compiled-in default next to what we actually use
    # makes the difference obvious — inside the bundle the former points at a
    # framework directory that only ever existed on the build machine.
    try:
        import costpoint_mobile
        ctx = costpoint_mobile.default_ssl_context()
        loaded = ctx.cert_store_stats().get("x509_ca", 0)
        print(f"  openssl     default cafile: {ssl.get_default_verify_paths().openssl_cafile}")
        print(f"  ca bundle   {loaded} certificate authorities loaded")
        if loaded < 10:
            print("  CA FAIL     the trust store is empty — every HTTPS call will fail")
            ok = False
    except Exception as e:                          # noqa: BLE001
        print(f"  CA FAIL     {e}")
        ok = False
        ctx = None

    # An actual handshake, if the network allows one. A rejected certificate is
    # a packaging bug and fails the test; not being able to reach Apple at all
    # is somebody's proxy, and only worth a note.
    if ctx is not None:
        try:
            with urllib.request.urlopen("https://www.apple.com/", timeout=20, context=ctx) as r:
                print(f"  tls check   https://www.apple.com/ → {r.status}")
        except ssl.SSLCertVerificationError as e:
            print(f"  TLS FAIL    certificate rejected: {e}")
            ok = False
        except Exception as e:                      # noqa: BLE001
            print(f"  tls check   skipped, no route to apple.com ({e})")

    # A federal holiday the app relies on knowing about, loaded through the same
    # dynamic import that py2app can't see.
    try:
        from datetime import date
        found = planning.federal_holidays(2026).get(date(2026, 7, 3))
        print(f"  holidays    2026-07-03 → {found}")
        ok = ok and bool(found)
    except Exception as e:                          # noqa: BLE001
        print(f"  HOLIDAY FAIL {e}")
        ok = False

    print("selftest:", "ok" if ok else "FAILED")
    return 0 if ok else 1


if "--selftest" in sys.argv[1:]:
    raise SystemExit(selftest())

# Before importing anything that might have something to say: an app bundle has
# nowhere to print unless we give it somewhere.
planning.setup_logging()

import timesheet

timesheet.main()
