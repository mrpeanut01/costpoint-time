"""py2app build configuration for Costpoint Timesheet.app.

Don't run this directly — use packaging/build.sh, which creates the build venv,
calls py2app, signs the result and wraps it in a .dmg. Everything below is just
the bundle's shape.

    python setup.py py2app          # what build.sh runs for you
"""
import os
import sys

# Must be imported before anything reaches for distutils: Python 3.12 dropped it
# from the standard library, and setuptools supplies the replacement py2app uses.
import setuptools  # noqa: F401
from setuptools import setup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appversion import __version__

APP_NAME = "Costpoint Timesheet"

# universal2 so one .dmg covers Apple Silicon and Intel. Every compiled
# dependency (pyobjc) ships universal2 wheels, and the interpreter itself is the
# universal2 framework build from python.org — see packaging/build.sh.
ARCH = os.environ.get("COSTPOINT_ARCH", "universal2")

PLIST = {
    "CFBundleDisplayName": APP_NAME,
    "CFBundleExecutable": APP_NAME,
    "CFBundleIdentifier": "com.costpoint-timesheet.app",
    "CFBundleShortVersionString": __version__,
    "CFBundleVersion": __version__,
    "CFBundleSpokenName": "Costpoint Timesheet",
    "LSApplicationCategoryType": "public.app-category.productivity",

    # The whole app is one menu bar item: no Dock icon, no window, and it never
    # steals focus at login.
    "LSUIElement": True,

    "LSMinimumSystemVersion": "11.0",
    "NSHighResolutionCapable": True,
    "NSSupportsAutomaticGraphicsSwitching": True,
    "NSHumanReadableCopyright": "MIT licensed. See LICENSE inside this bundle.",
}

OPTIONS = {
    "arch": ARCH,
    "iconfile": "packaging/icon.icns",
    "plist": PLIST,
    "resources": ["LICENSE"],

    # A second executable in Contents/MacOS, from the same interpreter and the
    # same code: this is what the daily launch agent runs. See the file itself.
    "extra_scripts": ["costpoint-timesheet.py"],

    # Copied wholesale rather than left to the dependency graph. holidays reaches
    # its country modules through importlib.import_module, which no static
    # analysis can follow, and carries 700-odd compiled translations besides;
    # dateutil has its own zoneinfo data; and certifi.where() hands OpenSSL a
    # filesystem path to cacert.pem, which has to be a real file on disk and not
    # an entry in py2app's zip.
    "packages": ["holidays", "dateutil", "rumps", "certifi"],

    "includes": ["appversion", "objc", "Foundation", "AppKit",
                 "PyObjCTools", "PyObjCTools.AppHelper"],
    "excludes": ["tkinter", "test", "pydoc_data", "PyInstaller"],
}

setup(
    name=APP_NAME,          # decides both Costpoint Timesheet.app and CFBundleName
    version=__version__,
    app=["tray.py"],
    options={"py2app": OPTIONS},
    setup_requires=[],      # build.sh installs py2app into the build venv first
)
