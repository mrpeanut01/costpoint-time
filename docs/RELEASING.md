# Releasing

A release is a tag. Bump `appversion.py`, commit, tag, push:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

`.github/workflows/release.yml` then builds `Costpoint Timesheet.app`, signs it
if it can, wraps it in a `.dmg` and attaches that to a GitHub Release. The only
thing it refuses to publish is a tag that disagrees with
`appversion.__version__`.

Every push to a pull request builds the same `.dmg` and leaves it as a workflow
artifact, so packaging breaks show up in review rather than at a tag.

## Signing is optional

Without an Apple Developer ID the build is ad-hoc signed and the `.dmg` is named
`-unsigned`. That release still works — it is byte-for-byte the same app — but
macOS flags the download and won't open it until the user clears the flag:

```bash
xattr -dr com.apple.quarantine "/Applications/Costpoint Timesheet.app"
```

The workflow puts that command in the release notes when the build is unsigned,
and leaves it out when it isn't, so the notes always match what people will
actually hit. Until the flag is cleared, Gatekeeper also runs the app from a
translocated read-only copy, which the app detects and explains rather than
scheduling a daily job against a path that won't exist tomorrow.

**That's the whole difference.** Add the certificate and the notarization key
below and the same tag produces a `.dmg` that opens on a double-click, with no
change to the app, the workflow or the process.

---

## What the build actually does

`packaging/build.sh` is the whole of it — CI runs that script and nothing else,
so a release you build on your own Mac is the same release.

1. **Find a universal2 framework Python.** py2app copies the interpreter it was
   run with into the bundle, so this one decision settles both what the app runs
   on and which Macs it runs on. The build refuses anything else rather than
   producing an app that only works on the machine that built it.
2. **py2app** (`setup.py`) builds `dist/Costpoint Timesheet.app`, with two
   executables in `Contents/MacOS`: the menu bar app, and the `costpoint-timesheet`
   CLI the daily launch agent runs.
3. **Sign**, innermost first — see the comment at the top of
   `packaging/signing_order.py` for why `codesign --deep` isn't used.
4. **Selftest**: run the signed bundle's CLI with `--selftest`, which imports
   everything, checks the CA bundle is really there and does a TLS handshake.
   This is the step that catches a package py2app left out.
5. **Notarize the app**, then staple the ticket into the `.app` itself — so the
   first launch works with no network.
6. **Build the `.dmg`**, sign it, notarize it, staple it too.

## Turning signing on

You need an **Apple Developer Program** membership ($99/year); notarization isn't
available without one. Then, once:

1. **Create a Developer ID Application certificate.** Xcode → Settings →
   Accounts → Manage Certificates → **+** → Developer ID Application. Or the
   [Certificates page](https://developer.apple.com/account/resources/certificates/list).
2. **Export it as a `.p12`.** Keychain Access → My Certificates → right-click the
   *Developer ID Application* certificate → Export. Set a password; you'll need
   it again in a moment. Make sure you export the certificate **with its private
   key** — the row has to have a disclosure triangle.
3. **Create an App Store Connect API key** for notarization:
   [Users and Access → Integrations → App Store Connect API](https://appstoreconnect.apple.com/access/integrations/api).
   Role **Developer** is enough. Download the `AuthKey_XXXXXXXXXX.p8` — it is
   offered exactly once — and note the **Key ID** and the **Issuer ID**.

An Apple ID with an app-specific password works too, and `packaging/build.sh`
accepts it (`NOTARY_APPLE_ID` / `NOTARY_PASSWORD` / `NOTARY_TEAM_ID`), but an API
key is scoped to notarization and can be revoked on its own.

Once the secrets are in, the next tag is signed and notarized. The first one is
worth watching: signing and notarization are the part CI cannot exercise until a
certificate exists, so budget a round of **When it goes wrong** below.

## Repository secrets

Settings → Secrets and variables → Actions → New repository secret.

| Secret | What it is |
|---|---|
| `MACOS_CERTIFICATE_P12` | the exported `.p12`, base64 |
| `MACOS_CERTIFICATE_PASSWORD` | the password you set when exporting it |
| `NOTARY_KEY_P8` | the `AuthKey_*.p8`, base64 |
| `NOTARY_KEY_ID` | the key's ID, e.g. `ABCD1234EF` |
| `NOTARY_ISSUER_ID` | the issuer UUID from the same page |
| `MACOS_SIGNING_IDENTITY` | *optional* — pin the identity if the certificate holds more than one |

Base64 the two files with no line wrapping:

```bash
base64 -i certificate.p12          | pbcopy
base64 -i AuthKey_ABCD1234EF.p8    | pbcopy
```

The workflow imports the certificate into a throwaway keychain, writes the key to
a file under `RUNNER_TEMP` with `umask 077`, and deletes both in a step that runs
even when the build fails.

## Building on your own Mac

```bash
packaging/build.sh --adhoc          # no Apple account needed; unsigned
packaging/build.sh --no-notarize    # signed, but don't wait on Apple
packaging/build.sh                  # the real thing
```

It signs with the one *Developer ID Application* certificate it finds in your
keychain unless you pass `--identity`. For notarization, export the same three
values the workflow uses:

```bash
export NOTARY_KEY=~/keys/AuthKey_ABCD1234EF.p8
export NOTARY_KEY_ID=ABCD1234EF
export NOTARY_ISSUER=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
packaging/build.sh
```

## Checking a build

```bash
codesign --verify --strict --deep --verbose=2 "dist/Costpoint Timesheet.app"
codesign -d --entitlements - "dist/Costpoint Timesheet.app"
spctl --assess --type execute -vv "dist/Costpoint Timesheet.app"

xcrun stapler validate dist/Costpoint-Timesheet-*.dmg
spctl --assess --type open --context context:primary-signature -vv dist/Costpoint-Timesheet-*.dmg
```

`spctl` saying **accepted / source=Notarized Developer ID** is the thing to look
for. *Notarized* is what makes the difference between a double-click and a panel
telling the user the app can't be opened.

The strongest check is still the honest one: put the `.dmg` on a Mac that has
never seen the source, and open it.

## When it goes wrong

**`The binary is not signed with a valid Developer ID certificate`** — an
Apple *Development* certificate was exported instead of *Developer ID
Application*. They look alike in Keychain Access; only the latter can be
notarized.

**`The signature does not include a secure timestamp`** — something was signed
without `--timestamp`, usually because the build ran offline.

**Notarization succeeds, Gatekeeper still refuses** — the ticket wasn't stapled,
or the `.dmg` was rebuilt after stapling. `xcrun stapler validate` on the exact
file that was uploaded.

**Detailed notarization log**: `xcrun notarytool log <submission-id>` with the
same credentials. It names the offending file, which is usually one that missed
the hardened runtime.

**The app opens but can't sign in** — the CA bundle. Run
`"…/Contents/MacOS/costpoint-timesheet" --selftest`; `build.sh` runs the same
check before making the `.dmg`, so this shouldn't reach a release.

## The icon

`packaging/icon.icns` is committed, so building needs neither Pillow nor a Mac.
Regenerate it only when the artwork changes:

```bash
pip install Pillow && python packaging/make_icon.py
```
