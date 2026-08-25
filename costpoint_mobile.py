#!/usr/bin/env python3
"""
costpoint_mobile.py — Costpoint Mobile T&E REST client.

A thin, dependency-free (stdlib only) Python client for the same JSON backend
the Deltek Costpoint Mobile Time & Expense app uses. Reverse-engineered from the
app bundle; see docs/API_PROTOCOL.md for the wire details and docs/FINDINGS.md
for how it was discovered. Used by timesheet.py.

Architecture (all to one endpoint, …/cpshared/backend/jsonproxy.php):
  - handshake()  -> commonwrapper.php, no creds, returns server version
  - login()      -> serverMethod=login, returns a session (cookieData+ProcIdSeed)
  - api(reqs)    -> serverMethod=api, Costpoint result-set protocol batch

Session model: NOT standard HTTP cookies. The login response returns `cookieData`
and `ProcIdSeed` in its JSON body; we round-trip `cookieData` on every call and
embed `ProcIdSeed` in the api batch envelope, exactly as the app does.
"""
from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["CostpointMobile", "CostpointError", "LoginError", "MfaRequired",
           "SamlRequired"]


class CostpointError(RuntimeError):
    """Any non-success response from the backend."""


class LoginError(CostpointError):
    """Credentials/auth rejected by the server."""


class MfaRequired(CostpointError):
    """Server demands an MFA passcode; call login_mfa() to continue.

    `help_msg` is the server-provided prompt (e.g. which authenticator to use);
    `want_pin` indicates the server also expects a separate PIN field.
    """

    def __init__(self, help_msg="", want_pin=False):
        super().__init__(help_msg or "Server requires an MFA passcode.")
        self.help_msg = help_msg
        self.want_pin = want_pin


class SamlRequired(CostpointError):
    """Server requires the SSO/SAML login flow (serverMethod=loginSaml)."""


# ── result-set protocol constants (mobile_probe/API_PROTOCOL.md) ──────────────
class Rs:
    TIMESHEET = "TMMTIMESHEET"
    TIMESHEET_APPROVE = "TMMTIMESHEET_APPROVE"
    HEADER = "TMMTS"
    LINE = "TMMTS_TS_LINE"
    CHARGE_FAVE = "TMMTS_CHARGE_FAVE"


def default_ssl_context() -> ssl.SSLContext:
    """A verifying TLS context that also works from inside an .app bundle.

    ssl.create_default_context() with no arguments trusts whatever OpenSSL was
    compiled to look at — and the interpreter that gets bundled into
    Costpoint Timesheet.app was compiled to look inside its own framework, at a
    path that exists on the machine that built it and nowhere else. Every
    request would fail CERTIFICATE_VERIFY_FAILED on the user's Mac.

    certifi is the same Mozilla root list Homebrew's Python trusts, shipped
    alongside the app so the path is always there. It stays optional: without it
    this falls back to the interpreter's own store, which is correct for a
    normal `python timesheet.py` from a checkout.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    try:
        return ssl.create_default_context(cafile=certifi.where())
    except OSError:                       # a certifi with no readable bundle
        return ssl.create_default_context()


class CostpointMobile:
    DEFAULT_BASE_PATH = "/DeltekTouch/Costpoint/TE"
    SHARED = "cpshared"

    def __init__(self, host: str, system: str,
                 base_path: str = DEFAULT_BASE_PATH,
                 user_agent: str = "Costpoint/4.0 (Android 9; Mobile)",
                 timeout: int = 120, verbose: bool = False):
        self.host = host.replace("https://", "").replace("http://", "").strip("/")
        self.system = system
        self.base = f"https://{self.host}{base_path}"
        self.backend = f"{self.base}/{self.SHARED}/backend"
        self.user_agent = user_agent
        self.timeout = timeout
        self.verbose = verbose
        self._ctx = default_ssl_context()

        # session state (populated by login)
        self.server_version: str | None = None
        self.cookies: str | None = None
        self.mfa_login_cookie: str | None = None
        self.proc_id_seed: str | None = None
        self.login_data: dict | None = None

    # ── low-level transport ───────────────────────────────────────────────────
    def _log(self, *a):
        if self.verbose:
            print("[cp]", *a)

    def _raw_post(self, url: str, body: bytes, content_type: str,
                  extra_headers: dict | None = None) -> str:
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("User-Agent", self.user_agent)
        req.add_header("Content-Type", content_type)
        req.add_header("Accept", "*/*")
        for k, v in (extra_headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise CostpointError(f"HTTP {e.code} from {url}: {e.read()[:300]!r}") from e

    @staticmethod
    def _parse_proxy_response(text: str) -> dict:
        """jsonproxy.php returns URL-encoded JSON (the app does
        JSON.parse(decodeURIComponent(text).replace(/\\'/g,"'")))."""
        for candidate in (text, urllib.parse.unquote(text)):
            try:
                obj = json.loads(candidate.replace("\\'", "'"))
                break
            except (ValueError, json.JSONDecodeError):
                obj = None
        if obj is None:
            raise CostpointError(f"Unparseable proxy response: {text[:200]!r}")
        # `data` is itself URL-encoded+escaped JSON; decode it when present.
        if isinstance(obj, dict) and isinstance(obj.get("data"), str):
            inner = obj["data"]
            try:
                obj["data"] = json.loads(urllib.parse.unquote(inner).replace("\\\"", "\"").replace("\\'", "'"))
            except (ValueError, json.JSONDecodeError):
                pass  # leave as string if it isn't JSON
        return obj

    def _proxy(self, server_method: str, payload, *, headers=None,
               cookie_data=None) -> dict:
        """POST to jsonproxy.php with the standard form fields."""
        payload_str = payload if isinstance(payload, str) else json.dumps(payload)
        fields = {
            "requestType": "POST",
            "serverMethod": server_method,
            "payload": payload_str,
            "cookieData": cookie_data if cookie_data is not None else (self.cookies or "null"),
        }
        body = urllib.parse.urlencode(fields).encode()
        text = self._raw_post(f"{self.backend}/jsonproxy.php", body,
                              "application/x-www-form-urlencoded", headers)
        obj = self._parse_proxy_response(text)
        # round-trip session material exactly like the app
        if obj.get("cookieData"):
            self.cookies = obj["cookieData"]
        if obj.get("ProcIdSeed"):
            self.proc_id_seed = obj["ProcIdSeed"]
        return obj

    # ── 1. handshake (no credentials) ─────────────────────────────────────────
    def handshake(self) -> str:
        """Return the Touch server version; also fetches server-init info."""
        body = json.dumps({"requestKey": "severVersion", "sharedLocation": self.SHARED})
        ver = self._raw_post(f"{self.backend}/commonwrapper.php", body.encode(),
                             "application/json").strip()
        self.server_version = ver
        self._log("server version", ver)
        return ver

    def server_info(self) -> dict:
        body = json.dumps({"requestKey": "serverInitInfo", "sharedLocation": self.SHARED})
        text = self._raw_post(f"{self.backend}/commonwrapper.php", body.encode(),
                             "application/json")
        return json.loads(text)

    # ── 2. login ──────────────────────────────────────────────────────────────
    @staticmethod
    def _ver_ge(a: str | None, b: str) -> bool:
        pa = [int(x) for x in (a or "0.0.0.0").split(".")]
        pb = [int(x) for x in b.split(".")]
        return pa >= pb

    def login(self, user: str, password: str) -> dict:
        """Username/password login. Picks the transport variant by server
        version (≥2.2.1 → Basic-auth header + systemEncoded payload).

        Raises LoginError on rejected credentials, MfaRequired if the server
        asks for a second factor. On success returns the inner `data` dict and
        leaves a live session on self (cookies + proc_id_seed).
        """
        if self.server_version is None:
            self.handshake()

        headers = {}
        if self._ver_ge(self.server_version, "2.2.1.0"):
            payload = {"restAPI": 1, "systemEncoded": base64.b64encode(self.system.encode()).decode()}
            headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        elif self._ver_ge(self.server_version, "2.2.0.0"):
            payload = {"restAPI": 1, "system": self.system}
            headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        else:
            payload = {"restAPI": 1, "system": self.system, "userid": user, "userpswd": password}

        obj = self._proxy("login", payload, headers=headers, cookie_data="null")
        return self._finish_login(obj)

    def login_mfa(self, passcode: str, pin: str = "") -> dict:
        payload = {"restAPI": 1, "mfaCode": passcode, "mfaPin": pin}
        obj = self._proxy("loginMfa", payload)
        return self._finish_login(obj)

    def _finish_login(self, obj: dict) -> dict:
        """Interpret a login / loginMfa response.

        The inner `data` object drives everything (mirrors the app's
        `if (!jsonData.authenticated) { mfaCode||mfaPin → MFA; saml → SSO; ... }`):
          - authenticated:true                → success
          - mfaCode/mfaPin present            → MFA challenge (raise MfaRequired)
          - saml in (1,2)                      → SSO required (raise SamlRequired)
          - error[...]                         → rejected (raise LoginError)
        """
        if obj.get("error"):
            raise LoginError(_as_text(obj["error"]))
        data = obj.get("data")
        if isinstance(data, dict):
            if data.get("authenticated"):
                self.login_data = data
                self._log("login OK; ProcIdSeed", self.proc_id_seed)
                return data
            # not authenticated — figure out why
            if data.get("mfaCode") or data.get("mfaPin"):
                # The mfaLoginCookie must be carried into the loginMfa call.
                self.mfa_login_cookie = obj.get("cookieData") or self.cookies
                raise MfaRequired(help_msg=_as_text(data.get("mfaHelpMsg", "")),
                                  want_pin=bool(data.get("mfaPin")))
            if data.get("saml") in (1, 2):
                raise SamlRequired("This account uses SSO/SAML; use loginSaml flow.")
            raise LoginError(_as_text(data.get("error") or "Invalid login information."))
        # data wasn't a dict we recognise; treat truthy as success, else error
        if data:
            self.login_data = {"raw": data}
            return self.login_data
        raise LoginError("Empty/unrecognised login response.")

    @property
    def authenticated(self) -> bool:
        return bool(self.login_data) and bool(self.cookies)

    def logout(self) -> dict:
        payload = {"requestCd": 999, "sid": self.proc_id_seed}
        return self._proxy("logout", payload)

    # ── 3. result-set api (the data plane) ────────────────────────────────────
    def api(self, request_objs: list[dict]) -> dict:
        """Send a batch of RS request objects (build them with the static
        helpers below). Wraps them in {requests:[...], ProcIdSeed} and POSTs
        serverMethod=api."""
        if not self.authenticated:
            raise CostpointError("Not authenticated — call login() first.")
        envelope = {"requests": list(request_objs), "ProcIdSeed": self.proc_id_seed}
        return self._proxy("api", json.dumps(envelope))

    # ── RS request builders (mirror ApiRequestBuilder) ────────────────────────
    @staticmethod
    def open_app(app_id, wizard_mode=None):
        o = {"appId": app_id}
        if wizard_mode is not None:
            o["wizardMode"] = wizard_mode
        return {"openApp": o}

    @staticmethod
    def close_app(app_id):
        return {"closeApp": {"appId": app_id}}

    @staticmethod
    def open_rs(app_id, parent_rs_id, rs_id, lookup_object=None, child_no=None):
        o = {"appId": app_id, "parentRSId": parent_rs_id, "rsId": rs_id}
        if lookup_object is not None:
            o["lookupObjectId"] = lookup_object
        if child_no is not None:
            o["childNo"] = child_no
        return {"openRS": o}

    @staticmethod
    def get_rs_data(app_id, parent_rs_id, rs_id, parent_ctx_tree,
                    row_start=None, row_end=None, columns=None, row_filter=None):
        o = {"appId": app_id, "parentRSId": parent_rs_id, "rsId": rs_id,
             "parentCtxTree": parent_ctx_tree}
        if row_start is not None:
            o["rowRange"] = {"start": row_start, "end": row_end}
        if columns is not None:
            o["columnRange"] = columns
        if row_filter is not None:
            o["rowFilter"] = row_filter
        return {"getRSData": o}

    @staticmethod
    def get_rs_metadata(app_id, parent_rs_id, rs_id, parent_ctx_tree):
        return {"getRSMetadata": {"appId": app_id, "parentRSId": parent_rs_id,
                                  "rsId": rs_id, "parentCtxTree": parent_ctx_tree}}

    @staticmethod
    def query_rs_data(app_id, parent_rs_id, rs_id, parent_ctx_tree,
                      sort=None, where=None, lookup_object=None):
        """where/sort follow the app's nesting: pass `where` as a list of
        condition dicts (this method wraps it as [where]); `sort` as a list of
        sort dicts. Build conditions/sorts with query_cond()/sort_by()."""
        o = {"appId": app_id, "parentRSId": parent_rs_id, "rsId": rs_id,
             "parentCtxTree": parent_ctx_tree}
        if sort is not None:
            o["sort"] = sort
        if where is not None:
            o["where"] = [where]
        if lookup_object is not None:
            o["lookupObjectId"] = lookup_object
        return {"queryRSData": o}

    @staticmethod
    def query_cond(object_id, value, operator="="):
        return {"objectId": object_id, "operator": operator, "value": value}

    @staticmethod
    def sort_by(object_id, order="asc"):
        return {"objectId": object_id, "order": order}

    @staticmethod
    def put_rs_data(app_id, parent_rs_id, rs_id, parent_ctx_tree, rs_data: list):
        return {"putRSData": {"appId": app_id, "parentRSId": parent_rs_id,
                              "rsId": rs_id, "parentCtxTree": parent_ctx_tree,
                              "rsData": rs_data}}

    @staticmethod
    def validate_field(app_id, parent_rs_id, rs_id, parent_ctx_tree, row_no, object_id=None):
        o = {"appId": app_id, "parentRSId": parent_rs_id, "rsId": rs_id,
             "rowNo": row_no, "parentCtxTree": parent_ctx_tree}
        if object_id is not None:
            o["objectId"] = object_id
        return {"validateField": o}

    @staticmethod
    def save_app(app_id, warnings_ok="1"):
        o = {"appId": app_id}
        if warnings_ok is not None:
            o["warningsOk"] = warnings_ok
        return {"saveApp": o}

    @staticmethod
    def run_action(app_id, action_id, parent_rs_id=None, rs_id=None,
                   parent_ctx_tree=None, row_no=None):
        o = {"appId": app_id, "actionId": action_id}
        if parent_rs_id is not None:
            o["parentRSId"] = parent_rs_id
        if rs_id is not None:
            o["rsId"] = rs_id
        if parent_ctx_tree is not None:
            o["parentCtxTree"] = parent_ctx_tree
        if row_no is not None:
            o["rowNo"] = row_no
        return {"runAction": o}

    @staticmethod
    def rs_row(row_no, status, fields: dict):
        """One putRSData row: {rowNo, status:[...], data:[{k:v},...]}.
        `status` may be a list or a single str ('updated'|'new'|'deleted'|'selected')."""
        if isinstance(status, str):
            status = [status]
        return {"rowNo": row_no, "status": status,
                "data": [{k: v} for k, v in fields.items()]}


def _as_text(err) -> str:
    if isinstance(err, (list, tuple)):
        return "; ".join(str(e) for e in err)
    return str(err)


if __name__ == "__main__":
    # Smoke test: handshake only (safe, no credentials).
    import os
    host = os.environ.get("COSTPOINT_HOST")
    system = os.environ.get("COSTPOINT_ORGANIZATION", "")
    if not host:
        raise SystemExit("Set COSTPOINT_HOST (e.g. yourcompany-cp.costpointfoundations.com)")
    cp = CostpointMobile(host, system, verbose=True)
    print("server version:", cp.handshake())
    print("server info:", json.dumps(cp.server_info(), indent=2))
