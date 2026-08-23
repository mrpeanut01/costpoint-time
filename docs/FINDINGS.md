# Mobile / REST Interface POC — Findings

Sibling POC to `soap_probe/` (WIC SOAP). Question asked: **can we reach and drive
the Costpoint Mobile T&E REST interface** described in the spec, as a faster
replacement for the Playwright browser automation in `../timesheet.py`?

**Short answer: YES — the interface is fully reachable and the protocol works
end-to-end.** The only thing not yet proven is a *successful authenticated
session*, and that is blocked by a credential/account issue that **also blocks
the existing web login** — not by anything about the API.

Tenant tested: `<company>-cp.costpointfoundations.com` (any Costpoint
Foundations tenant follows the same shape).
Touch server version (live): **2025.1.0.1**

---

## 1. The spec's endpoint names are wrong (fabricated/inferred)

Every per-operation filename in the spec is a **404** on this tenant:

| Spec endpoint            | Result |
|--------------------------|--------|
| `login.php`              | 404 |
| `getTimesheet.php`       | 404 |
| `saveTimesheetHours.php` | 404 |
| `signTimesheet.php`      | 404 |
| `getPayPeriods.php`, `getChargeCodes.php`, `getLeaveBalances.php` | 404 |

The spec's auth model (form-encoded `username=&password=`, `PHPSESSID`) is also
wrong. Treat that document as **unreliable** — it was clearly written from
guesswork, not real traffic. The real protocol below was reverse-engineered from
the app's own JS bundle (`captured/app.js`, 7.3 MB, Sencha Ext JS).

## 2. The real architecture

The Touch T&E layer **is deployed** here. It is a single-page app that talks to
**one JSON proxy endpoint**, not per-operation files:

```
BASE = https://<host>/DeltekTouch/Costpoint/TE
  <BASE>/cpshared/backend/commonwrapper.php   ← version + server-info handshake (no auth)
  <BASE>/cpshared/backend/jsonproxy.php        ← EVERYTHING else (login, api, logout)
  <BASE>/cpshared/backend/samltokenreturn.php  ← SAML/SSO return page (deep-links the native app)
  <BASE>/cpshared/backend/cptimeurl.php        ← just a deep-link launcher for the native app
```

`jsonproxy.php` is a JSON-RPC-style gateway. The operation is selected by a
`serverMethod` form field; the real request rides in a `payload` JSON string.
Observed `serverMethod` values: `login`, `loginMfa`, `loginSaml`, `api`, `logout`.

## 3. Protocol (verified live)

### Handshake — `commonwrapper.php`  (no credentials, confirmed working)
Raw JSON body (note Deltek's own typo `severVersion`):
```json
{"requestKey":"severVersion","sharedLocation":"cpshared"}        → "2025.1.0.1"
{"requestKey":"serverInitInfo","sharedLocation":"cpshared"}      → {serverVersion, pinRules,
                                                                    mobileSessionTimeoutMins:15, ...}
```
`serverInitInfo` shows **no SAML-enforced flag** and a 15-minute session timeout.

### Login — `jsonproxy.php`  (endpoint confirmed working; auth rejected)
Form-encoded POST. For server ≥ 2.2.1 (our case):
```
requestType  = POST
serverMethod = login
payload      = {"restAPI":1,"systemEncoded":base64("<ORGANISATION>")}
cookieData   = null
header:  Authorization: Basic base64("<user>:<password>")
```
Live response (HTTP 200, structured JSON — proves the endpoint fully processes the request):
```json
{
  "cookieData": "JSESSIONID=...!...;autologin=0;cpSession=0",
  "data": "{\"authenticated\":false,\"error\":[\"Invalid Login information entered. ...\"]}"
}
```
The legacy transport (`{...,"userid","userpswd"}` in the payload, no header) returns
the **identical** rejection — so the request shape is correct in both forms; the
**credential tuple** is what's refused.

### Authenticated API calls (next step, once login succeeds)
```
serverMethod = api
payload      = <apiRequestJson>
cookieData   = <session cookies from login>
```

## 4. Why login is rejected — NOT an API problem

The same credentials in `../.env` **also fail the official web login.** Screenshot
`../screenshots/03-post-login.png`, taken *after* the web "Log In" click, still
shows the login form (with "Use Passkey"), i.e. the browser flow did not get a
session either. So the mobile POC is faithfully reproducing the *same* auth result
as Deltek's own channels. The blocker is one (or more) of:

- the password in `.env` is **stale / expired**, or
- the account requires **passkey / MFA** (the web UI offers "Use Passkey"; the
  mobile API has a `loginMfa` path), or
- login needs extra criteria (the web form hides a "More Criteria"/database field).

None of these are limitations of the REST interface.

## 5. Verdict for the automation

- **Feasible and clearly better than Playwright**: 3 small JSON POSTs (handshake →
  login → api) vs. a headless Chromium driving a flaky DOM. No browser, no
  screenshots, ~100× lighter in CI.
- **Blocker to clear first:** get one valid interactive login working (confirm the
  current password; determine whether passkey/MFA is mandatory). Once a plain
  password login succeeds in the browser, the mobile `login` call will succeed too
  and we can map the `serverMethod=api` payloads for read/save/sign by capturing a
  few real app requests (or by reading the relevant Ext.js stores in `captured/app.js`).
- **If the org enforces SSO/passkey**, use `serverMethod=loginSaml` (SAMLResponse
  handoff) — more work — or fall back to the WIC SOAP POC.

## Note on provenance
This was reverse-engineered with a set of throwaway probe scripts (a reachability
sweep, an endpoint-discovery pass that downloaded the SPA bundle `app.js`, and
step-by-step login/load/add-line explorers). Those scripts and the downloaded
`app.js` were removed once the protocol was distilled into `../costpoint_mobile.py`
and `API_PROTOCOL.md`. To re-probe after a Costpoint upgrade, re-capture `app.js`
from `…/DeltekTouch/Costpoint/TE/app.js` and grep it for the request builders.
