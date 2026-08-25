"""The one place the app's version number is written down.

`setup.py` stamps it into the bundle's Info.plist, the tray shows it under
About, and the release workflow refuses to build if the git tag disagrees —
so cutting a release is: bump this, commit, tag `v<same number>`, push.

(Named `appversion` rather than the obvious `version` because py2app flattens
every module into one zip, where a name that common is asking to be shadowed.)
"""

__version__ = "1.0.0"
