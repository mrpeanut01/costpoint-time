#!/usr/bin/env python3
"""List everything inside an .app that has to be signed, innermost first.

Signatures nest: sealing a bundle records a hash of everything under it, so
anything signed afterwards invalidates the seal above it. `codesign --deep`
claims to handle that and is both deprecated and wrong — it can't apply
entitlements to nested code — so build.sh signs each piece itself, in the order
printed here, and the .app last.

Two kinds of thing need a signature of their own:

  * every Mach-O file — the 200-odd extension modules under lib-dynload, the
    dylibs py2app copies in, and the launcher stubs in Contents/MacOS;
  * every nested code bundle — Python.framework (by its versioned directory,
    which is what codesign will accept) and the Python.app helper inside it.

Output is NUL-separated for `xargs -0`, deepest path first. The bundle passed in
is never itself listed: build.sh signs that one last, with the entitlements.

    python3 packaging/signing_order.py "dist/Costpoint Timesheet.app"
"""
from __future__ import annotations

import os
import sys

# Mach-O and universal ("fat") binary magic numbers, both byte orders.
MACHO_MAGIC = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",      # 32-bit
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",      # 64-bit
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",      # universal
}

BUNDLE_SUFFIXES = (".app", ".framework", ".xpc", ".bundle", ".plugin")


def is_macho(path: str) -> bool:
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as fh:
            return fh.read(4) in MACHO_MAGIC
    except OSError:
        return False


def bundle_targets(path: str) -> list[str]:
    """What codesign will accept for a nested bundle.

    A framework has to be signed one version at a time — handing codesign the
    .framework itself fails on the Current/ symlink — and everything else is
    signed as a whole.
    """
    versions = os.path.join(path, "Versions")
    if path.endswith(".framework") and os.path.isdir(versions):
        return [os.path.join(versions, v) for v in sorted(os.listdir(versions))
                if v != "Current" and os.path.isdir(os.path.join(versions, v))
                and not os.path.islink(os.path.join(versions, v))]
    return [path]


def collect(root: str) -> list[str]:
    root = os.path.abspath(root).rstrip("/")
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not os.path.islink(os.path.join(dirpath, d))]
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if name.endswith(BUNDLE_SUFFIXES) and full != root:
                found.extend(bundle_targets(full))
        for name in filenames:
            full = os.path.join(dirpath, name)
            if is_macho(full):
                found.append(full)

    # Deepest first, so a nested bundle is sealed only once everything it
    # contains already carries a signature. Path as the tiebreaker keeps the
    # order stable between builds.
    return sorted(set(found), key=lambda p: (-p.count(os.sep), p))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print(f"usage: {os.path.basename(argv[0])} <path-to-.app>", file=sys.stderr)
        return 2
    if not os.path.isdir(argv[1]):
        print(f"not a bundle: {argv[1]}", file=sys.stderr)
        return 1
    sys.stdout.write("\0".join(collect(argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
