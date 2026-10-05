#!/usr/bin/env python3
"""
recorder.py — stand between an app and its real API, and write down what happens.

The document says what the API is supposed to do. The only record of what it
actually does is the traffic, and nobody reads traffic. Point an app (or curl,
or Postman) at this instead of at the server: every request is passed straight
through and answered by the real API, and each exchange is noted. observe.py
then says where the real API and the document disagree, which documented
endpoints the app really uses, and turns what was clicked through into a test.

    python recorder.py --env dev                       # a server set up in mockd
    python recorder.py --to https://api.dev.example.com --port 4011

What is written down: the method, path, query, JSON body, status and JSON
answer. What is never written down: any header — so no token, no cookie, no
password. A request that arrives without credentials is signed in as the user
set up for that server, so plain curl works; one that brings its own is passed
on untouched. A server marked read only is only ever read from.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, Response, jsonify, request
from flask_cors import CORS

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "logs" / "recorded.jsonl"
KEEP_BODY = 256 * 1024                      # a larger answer is noted, not kept
SKIP_REQUEST = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
PASS_BACK = ("content-type", "location", "content-disposition", "etag", "cache-control",
             "www-authenticate")
WRITES = ("POST", "PUT", "PATCH", "DELETE")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):       # the caller follows, not us
        return None


def _json_or_none(raw, content_type=""):
    if not raw or len(raw) > KEEP_BODY:
        return None
    if "json" not in (content_type or "").lower() and raw[:1] not in (b"{", b"["):
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


def build_app(upstream, out_path=DEFAULT_OUT, sign_in=None, read_only=False, label=None):
    """`sign_in` is a callable returning headers for a request that brought no
    credentials of its own; it is called lazily, and again if a call is refused."""
    upstream = upstream.rstrip("/")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(_NoRedirect)
    lock = threading.Lock()
    state = {"count": 0, "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "to": upstream, "label": label or upstream, "auth": None}

    app = Flask("mockd-recorder")
    CORS(app, supports_credentials=True)

    def note(entry):
        with lock:
            state["count"] += 1
            with out_path.open("a") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @app.get("/_recorder/state")
    def recorder_state():
        return jsonify({k: v for k, v in state.items() if k != "auth"})

    @app.route("/", defaults={"path": ""},
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    @app.route("/<path:path>",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    def through(path):
        if request.method == "OPTIONS":
            return "", 204                                  # a browser asking permission
        full = "/" + path
        if read_only and request.method in WRITES:
            return jsonify({"recorder": f"{state['label']} is marked read only, so "
                                        f"{request.method} was not passed on"}), 403

        headers = {k: v for k, v in request.headers.items() if k.lower() not in SKIP_REQUEST}
        headers["Accept-Encoding"] = "identity"
        own = any(k.lower() in ("authorization", "cookie") for k in headers)
        if not own and sign_in is not None:
            if state["auth"] is None:
                try:
                    state["auth"] = sign_in() or {}
                except Exception:
                    state["auth"] = {}
            headers.update(state["auth"])

        body = request.get_data() if request.method in WRITES else None
        url = upstream + full + (("?" + request.query_string.decode()) if request.query_string else "")
        started = time.time()
        try:
            call = urllib.request.Request(url, data=body, headers=headers, method=request.method)
            try:
                with opener.open(call, timeout=60) as resp:
                    status, raw, got = resp.status, resp.read(), resp.headers
            except urllib.error.HTTPError as exc:
                status, raw, got = exc.code, exc.read(), exc.headers
        except Exception as exc:
            return jsonify({"recorder": f"could not reach {state['label']}: {exc}"}), 502
        took = int((time.time() - started) * 1000)
        if status == 401 and not own:
            state["auth"] = None                            # sign in afresh next time

        content_type = got.get("Content-Type", "")
        note({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "method": request.method, "path": full, "query": request.args.to_dict(),
              "request_body": _json_or_none(body, request.headers.get("Content-Type", "")),
              "status": status, "content_type": content_type.split(";")[0].strip(),
              "response": _json_or_none(raw, content_type), "bytes": len(raw), "ms": took})

        reply = Response(raw, status=status)
        for name in PASS_BACK:
            if got.get(name):
                reply.headers[name] = got.get(name)
        for cookie in got.get_all("Set-Cookie") or []:
            # A cookie scoped to the server's domain is refused by a browser that
            # is talking to localhost; without the scope it is kept, and the app
            # stays signed in while its traffic is being watched.
            reply.headers.add("Set-Cookie", re.sub(r";\s*Domain=[^;]*", "", cookie, flags=re.I))
        return reply

    return app


def main():
    ap = argparse.ArgumentParser(description="Pass traffic through to a real API and record it")
    ap.add_argument("--to", help="the real API's address")
    ap.add_argument("--env", help="a server set up in mockd, instead of --to")
    ap.add_argument("--port", type=int, default=4011)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    sign_in, read_only, label = None, False, args.to
    if args.env:
        sys.path.insert(0, str(HERE))
        import environments as envmod
        env = envmod.get(args.env)
        args.to = envmod.resolve(env.get("base_url", ""), [])
        read_only, label = bool(env.get("readonly")), args.env
        sign_in = lambda: envmod.authenticate(env)[0]       # noqa: E731
    if not args.to or not args.to.lower().startswith(("http://", "https://")):
        ap.error("give --to an http(s) address, or --env a server that has one")

    app = build_app(args.to, args.out, sign_in=sign_in, read_only=read_only, label=label)
    print(f"recording: http://{args.host}:{args.port}  ->  {label}", flush=True)
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
