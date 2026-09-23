#!/usr/bin/env python3
"""
environments.py — named targets (base URL + how to authenticate), shared by the
whole team.

QA should not be pasting a base URL and a token into three different tools. An
environment is declared once, by name, and `verify.py`, `postman.py` and the
console all take `--env dev`.

    python environments.py list
    python environments.py show dev
    python environments.py login dev          # prove the credentials work

Secrets never live in this file. Every value may be written as ${VAR}, resolved
from the process environment or a local .env, so `environments.json` holds the
SHAPE of an environment and is safe to commit, while the password lives in the
shell, in CI's secret store, or in environments.local.json — which is
gitignored and merged on top of the committed file.

Three ways to authenticate:

  none    the mock, unless it was started with --require-auth
  token   a token you already have: sent as a header, Bearer by default
  login   call a login endpoint and use what it gives back — a token read out
          of the JSON, or the cookies it sets, which is what this API does
          (/api/v1/auth/session takes username+password as query params and
          replies with Set-Cookie, no bearer token anywhere)
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_FILE = HERE / "environments.json"
LOCAL_FILE = HERE / "environments.local.json"
DOTENV = HERE / ".env"

SECRET_HINTS = ("password", "token", "secret", "authorization", "cookie", "apikey")
# keys that merely MENTION a secret without holding one
NOT_SECRET_SUFFIXES = ("_path", "_field", "_header", "_mode", "_type")
NOT_SECRET_KEYS = {"mode", "prefix", "header", "send", "method", "description",
                   "base_url", "path", "use_cookies", "token_path"}


def is_secret(key, value):
    if not isinstance(value, str) or not value:
        return False                    # booleans and numbers are never secrets
    k = key.lower()
    if k in NOT_SECRET_KEYS or k.endswith(NOT_SECRET_SUFFIXES):
        return False
    return any(hint in k for hint in SECRET_HINTS)


# ----------------------------------------------------------------------------
# Loading and ${VAR} resolution
# ----------------------------------------------------------------------------


def _dotenv():
    values = {}
    if DOTENV.exists():
        for line in DOTENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip().strip('"').strip("'")
    return values


class Unresolved(Exception):
    """A ${VAR} the environment does not define — better to say so than to send
    the literal string '${DEV_PASSWORD}' at a login endpoint."""


def resolve(value, missing=None):
    if isinstance(value, dict):
        return {k: resolve(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, missing) for v in value]
    if not isinstance(value, str):
        return value
    env = {**_dotenv(), **os.environ}

    def swap(match):
        name = match.group(1)
        if name in env:
            return env[name]
        if missing is not None:
            missing.append(name)
            return ""
        raise Unresolved(
            f"${{{name}}} is not set. Export it in your shell, add it to .env, "
            f"or set the value directly in environments.local.json")
    return re.sub(r"\$\{(\w+)\}", swap, value)


def _merge(base, extra):
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path=None):
    """Committed file, with the gitignored local file merged on top."""
    path = Path(path) if path else DEFAULT_FILE
    doc = json.loads(path.read_text()) if path.exists() else {"environments": {}}
    envs = doc.get("environments", doc)
    if LOCAL_FILE.exists():
        local = json.loads(LOCAL_FILE.read_text())
        envs = _merge(envs, local.get("environments", local))
    return envs


def get(name, path=None):
    envs = load(path)
    if name not in envs:
        raise SystemExit(f"no environment named {name!r}. Known: {', '.join(sorted(envs)) or 'none'}")
    return envs[name]


def variables_in(node, out=None):
    """Every ${VAR} an environment refers to, in the order they appear."""
    out = [] if out is None else out
    if isinstance(node, dict):
        for value in node.values():
            variables_in(value, out)
    elif isinstance(node, list):
        for value in node:
            variables_in(value, out)
    elif isinstance(node, str):
        for name in re.findall(r"\$\{(\w+)\}", node):
            if name not in out:
                out.append(name)
    return out


def write_dotenv(values, path=None):
    """Merge values into .env, keeping everything already there.

    .env rather than the committed file, because this is where secrets belong:
    it is gitignored, it is read at resolution time so a change takes effect
    without a restart, and nothing typed here can end up in a pull request."""
    path = Path(path) if path else DOTENV
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        name = stripped.split("=", 1)[0].strip()
        if name in values:
            out.append(f"{name}={values[name]}")
            seen.add(name)
        else:
            out.append(line)
    for name, value in values.items():
        if name not in seen:
            out.append(f"{name}={value}")
    path.write_text("\n".join(out).rstrip("\n") + "\n")
    return path


def mask(value):
    text = str(value)
    return text if len(text) <= 8 else text[:4] + "…" + text[-4:]


# ----------------------------------------------------------------------------
# Authentication
# ----------------------------------------------------------------------------


def _request(method, url, headers=None, body=None, form=None, timeout=30):
    data = None
    headers = dict(headers or {})
    if method.upper() in ("POST", "PUT", "PATCH") and body is None and form is None:
        # a POST carrying its arguments in the query string still has a body —
        # an empty one. Without it urllib sends no Content-Length and strict
        # servers answer 411 Length Required.
        data = b""
        headers.setdefault("Content-Length", "0")
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def _dig(payload, path):
    """'data.access_token' out of a nested response."""
    node = payload
    for part in str(path).split("."):
        if isinstance(node, list):
            try:
                node = node[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _cookies_from(headers_obj, raw_headers):
    """Collect every Set-Cookie into one Cookie header."""
    values = []
    for name, value in (raw_headers or {}).items():
        if name.lower() == "set-cookie":
            values.append(value)
    jar = []
    for value in values:
        for chunk in re.split(r",(?=[^;=]+=)", value):
            pair = chunk.split(";")[0].strip()
            if "=" in pair:
                jar.append(pair)
    return "; ".join(jar)


def authenticate(env, verbose=False):
    """Return (headers, note). Performs a login when the environment asks for one."""
    auth = resolve(env.get("auth") or {"mode": "none"})
    mode = auth.get("mode", "none")
    headers = resolve(env.get("headers") or {})

    if mode == "none":
        return headers, "no authentication"

    if mode == "token":
        token = auth.get("token", "")
        if not token:
            raise SystemExit("auth.mode is 'token' but no token resolved — "
                             "set the ${VAR} it refers to")
        name = auth.get("header", "Authorization")
        prefix = auth.get("prefix", "Bearer ")
        headers[name] = f"{prefix}{token}" if prefix and not token.startswith(prefix) else token
        return headers, f"static token in {name}"

    if mode != "login":
        raise SystemExit(f"unknown auth.mode {mode!r} (none | token | login)")

    base = resolve(env.get("base_url", "")).rstrip("/")
    path = auth.get("path") or "/"
    method = (auth.get("method") or "POST").upper()
    where = (auth.get("send") or "query").lower()
    creds = {auth.get("username_field", "username"): auth.get("username", ""),
             auth.get("password_field", "password"): auth.get("password", "")}
    if not creds[auth.get("username_field", "username")]:
        raise SystemExit("auth.mode is 'login' but no username resolved — "
                         "set the ${VAR} it refers to")

    url = base + path
    body = form = None
    if where == "query":
        url += "?" + urllib.parse.urlencode(creds)
    elif where == "form":
        form = creds
    else:
        body = creds

    if verbose:
        shown = {k: (mask(v) if "pass" in k else v) for k, v in creds.items()}
        print(f"  POST {base + path}  ({where}: {shown})")

    status, raw_headers, payload = _request(method, url, headers, body, form)
    if status >= 400:
        raise SystemExit(f"login failed: {status} {payload[:300].decode(errors='replace')}")

    note_parts = []
    if auth.get("token_path"):
        try:
            parsed = json.loads(payload or b"null")
        except ValueError:
            parsed = None
        token = _dig(parsed, auth["token_path"]) if parsed is not None else None
        if token:
            name = auth.get("header", "Authorization")
            prefix = auth.get("prefix", "Bearer ")
            headers[name] = f"{prefix}{token}"
            note_parts.append(f"token from {auth['token_path']}")

    if auth.get("use_cookies", True):
        cookie = _cookies_from(None, raw_headers)
        if cookie:
            headers["Cookie"] = cookie
            note_parts.append(f"{cookie.count('=')} cookie(s)")

    if not note_parts:
        raise SystemExit(
            f"login returned {status} but nothing usable: no cookies were set and "
            f"token_path {auth.get('token_path')!r} did not match. "
            f"Body: {payload[:200].decode(errors='replace')}")
    return headers, "login -> " + ", ".join(note_parts)


def headers_for(name, path=None, verbose=False):
    """A missing credential is an ordinary, expected situation — report it as a
    sentence, not as a stack trace from three frames down."""
    env = get(name, path)
    try:
        headers, note = authenticate(env, verbose)
        base = resolve(env.get("base_url", ""))
    except Unresolved as exc:
        raise SystemExit(f"environment {name!r}: {exc}")
    return base, headers, note


def base_url_for(name, path=None):
    """The address only — never performs a login."""
    return resolve(get(name, path).get("base_url", ""), []).rstrip("/")


def data_for(name, path=None):
    """Test data that belongs to THIS environment.

    A suite's own `data` says what a value means; an environment's `data` says
    what it is on that server. The id of a department that exists on staging is
    not the id of one on dev, and neither belongs in a committed test file."""
    env = get(name, path)
    return resolve(env.get("data") or {}, [])


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def _describe(name, env):
    auth = (env.get("auth") or {}).get("mode", "none")
    missing = []
    resolve(env, missing)
    flag = f"  [unresolved: {', '.join(sorted(set(missing)))}]" if missing else ""
    print(f"  {name:12s} {env.get('base_url', ''):42s} auth={auth}{flag}")


def main():
    ap = argparse.ArgumentParser(description="Named environments for the mock and real APIs")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show every environment and whether its secrets resolve")
    show = sub.add_parser("show", help="print one environment with secrets masked")
    show.add_argument("name")
    login = sub.add_parser("login", help="perform the login and show the headers it yields")
    login.add_argument("name")
    args = ap.parse_args()

    envs = load()
    if args.command == "list":
        if not envs:
            print(f"no environments defined — create {DEFAULT_FILE.name}")
            return 0
        print(f"{len(envs)} environment(s) in {DEFAULT_FILE.name}"
              + (f" + {LOCAL_FILE.name}" if LOCAL_FILE.exists() else ""))
        for name, env in sorted(envs.items()):
            _describe(name, env)
        return 0

    env = get(args.name)
    if args.command == "show":
        def hide(node, key=""):
            if isinstance(node, dict):
                return {k: hide(v, k) for k, v in node.items()}
            if isinstance(node, list):
                return [hide(v, key) for v in node]
            if is_secret(key, node):
                resolved = resolve(node, [])
                return mask(resolved) if resolved else node + "  (unresolved)"
            return node
        print(json.dumps(hide(env), indent=2))
        return 0

    print(f"logging in to {args.name} ({resolve(env.get('base_url', ''), [])})")
    headers, note = authenticate(env, verbose=True)
    print(f"  ok — {note}")
    for name, value in headers.items():
        print(f"  {name}: {mask(value) if is_secret(name, value) else value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
