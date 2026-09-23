#!/usr/bin/env python3
"""
postman.py — turn the spec into a Postman collection aimed at the mock.

QA's first move is usually "let me poke it by hand", and the last move is "run
it in CI". A collection serves both: import it into Postman today, run the same
file with `newman` tomorrow. Nothing here is mock-specific except the default
`baseUrl` — repoint that variable and the same collection exercises dev, staging
or production.

    python postman.py --spec apis.json --base-url http://localhost:4010 \\
        --out mockd.postman_collection.json

    newman run mockd.postman_collection.json --env-var baseUrl=https://api.dev.example.com

What each request carries:

  * a body generated from the operation's request schema — valid, so it passes
  * required query params filled in; optional ones present but disabled, so they
    are one click away instead of one lookup away
  * path params as real Postman path variables
  * the X-Mock-* control headers, disabled, so a tester can force a 500 or an
    empty list without reading any documentation
  * tests asserting the status is one the spec documents, that the body parses,
    and that the documented top-level fields are present — the seed of the
    automated suite rather than a placeholder for it
"""
import argparse
import json
import random
import re
import project
from pathlib import Path

from generator import generate_from_schema
from mockd import Source, Spec, _success_status, operation_coverage

CONTROL_HEADERS = [
    ("X-Mock-Status", "{{mockStatus}}", "force a documented status, e.g. 404 or 500"),
    ("X-Mock-Scenario", "{{mockScenario}}", "a named scenario, e.g. empty or page_full"),
    ("X-Mock-Nulls", "{{mockNulls}}", "'on' nulls every nullable field"),
    ("X-Mock-Delay", "{{mockDelay}}", "delay in ms, for loader and timeout tests"),
]


def sample_value(schema, rng, fallback="1"):
    value = generate_from_schema(schema or {"type": "string"}, rng)
    if value is None or isinstance(value, (dict, list)):
        return fallback
    return str(value)


def url_for(route, base, rng):
    """Postman path variables (:user_id) rather than OpenAPI braces."""
    path = re.sub(r"\{([^}]+)\}", r":\1", route["path"])
    segments = [s for s in path.strip("/").split("/") if s]

    variables, query = [], []
    for p in route["parameters"]:
        schema = p.get("schema") or {}
        if p.get("in") == "path":
            variables.append({"key": p["name"], "value": sample_value(schema, rng),
                              "description": p.get("description", "")})
        elif p.get("in") == "query":
            item = {"key": p["name"], "value": sample_value(schema, rng, ""),
                    "description": p.get("description", "")}
            if not p.get("required"):
                item["disabled"] = True          # visible, but not sent by default
            query.append(item)

    url = {"raw": "{{baseUrl}}/" + "/".join(segments), "host": ["{{baseUrl}}"],
           "path": segments}
    if query:
        url["query"] = query
        enabled = [q for q in query if not q.get("disabled")]
        if enabled:
            url["raw"] += "?" + "&".join(f"{q['key']}={q['value']}" for q in enabled)
    if variables:
        url["variable"] = variables
    return url


def tests_for(route):
    """Assertions every request can make from the spec alone."""
    documented = sorted(int(c) for c in route["responses"] if str(c).isdigit())
    success = _success_status(route["responses"])
    schema = (((route["responses"].get(success) or {}).get("content") or {})
              .get("application/json") or {}).get("schema") or {}
    required = [k for k in (schema.get("properties") or {})
                if k in (schema.get("required") or [])]

    lines = [
        "// Generated from the OpenAPI document. Extend freely — this file is the",
        "// starting point for the automated suite, not a throwaway.",
        f"const documented = {json.dumps(documented)};",
        "",
        "pm.test('status is documented in the spec', function () {",
        "    pm.expect(documented).to.include(pm.response.code);",
        "});",
        "",
        "pm.test('responds within 2s', function () {",
        "    pm.expect(pm.response.responseTime).to.be.below(2000);",
        "});",
    ]
    if documented and documented[0] < 300:
        lines += [
            "",
            "if (pm.response.code < 300) {",
            "    pm.test('body is JSON', function () { pm.response.json(); });",
        ]
        if required:
            lines += [
                f"    const required = {json.dumps(required)};",
                "    pm.test('success body has the documented top-level fields', function () {",
                "        const body = pm.response.json();",
                "        required.forEach((f) => pm.expect(body).to.have.property(f));",
                "    });",
            ]
        lines += ["}"]
    lines += [
        "",
        "// Where the mock got this body (overlay / spec:example / generated /",
        "// synthesized / stateful). Absent against a real backend.",
        "const source = pm.response.headers.get('X-Mock-Source');",
        "if (source) { pm.collectionVariables.set('lastMockSource', source); }",
    ]
    return lines


FULL = "\u2705"       # everything declared: explore it freely
PARTIAL = "\u26a0\ufe0f"  # readable but not checkable
THIN = "\u2b55"       # no declared response shape


def contract_note(route):
    """A one-line verdict a tester reads before opening the request, plus the
    counts that say how much there is to play with."""
    cov = operation_coverage(route)
    d = cov["detail"]

    bits = []
    if d["body_required"] or d["body_optional"]:
        bits.append(f"body: {d['body_required']} required / {d['body_optional']} optional")
    if d["query_required"] or d["query_optional"]:
        bits.append(f"query: {d['query_required']} required / {d['query_optional']} optional")
    if d["path_params"]:
        bits.append(f"{d['path_params']} path param(s)")
    bits.append("statuses you can force: " + ", ".join(d["forceable_statuses"]))
    counts = " \u00b7 ".join(bits)

    if cov["state"] == "complete":
        headline = (f"{FULL} FULLY SPECIFIED — the response schema is declared, so every "
                    f"field and type below is the real contract.")
    elif cov["state"] == "partial":
        headline = (f"{PARTIAL} PARTLY SPECIFIED — missing: {', '.join(cov['missing'])}. "
                    f"Readable, but nothing automated can check the response.")
    else:
        headline = (f"{THIN} RESPONSE SHAPE NOT DECLARED — the spec says nothing about what "
                    f"this returns, so the body you get back is INFERRED by the mock. "
                    f"Do not build assertions on its shape yet.")
    return cov, headline, counts


def request_item(route, spec, rng):
    headers = [{"key": "Accept", "value": "application/json"}]
    body = None
    media = (route["request_body"].get("content") or {}).get("application/json")
    if media and media.get("schema"):
        generated = generate_from_schema(media["schema"], rng, array_items=1)
        if generated is not None:
            headers.append({"key": "Content-Type", "value": "application/json"})
            body = {"mode": "raw",
                    "raw": json.dumps(generated, indent=2, ensure_ascii=False),
                    "options": {"raw": {"language": "json"}}}

    headers.append({"key": "Authorization", "value": "Bearer {{token}}", "disabled": True,
                    "description": "only enforced when the mock runs with --require-auth"})
    for name, value, description in CONTROL_HEADERS:
        headers.append({"key": name, "value": value, "disabled": True,
                        "description": description})

    cov, headline, counts = contract_note(route)
    description = "\n\n".join(filter(None, [
        route["summary"],
        headline,
        counts,
        "Enable X-Mock-Status to force a failure path, or X-Mock-Scenario for an "
        "empty list / full page.",
    ]))

    marker = {"complete": FULL, "partial": PARTIAL}.get(cov["state"], THIN)
    item = {
        "name": f"{marker} {route['summary'] or route['key']}",
        "request": {
            "method": route["method"],
            "header": headers,
            "url": url_for(route, "{{baseUrl}}", rng),
            "description": description,
        },
        "event": [{"listen": "test", "script": {"type": "text/javascript",
                                                "exec": tests_for(route)}}],
        "response": [],
    }
    if body:
        item["request"]["body"] = body
    return item


def build(spec, base_url):
    folders, states = {}, {"complete": 0, "partial": 0, "undocumented": 0}
    # fully specified first inside each folder — the ones worth exploring are at
    # the top of the sidebar rather than scattered alphabetically
    order = {"complete": 0, "partial": 1, "undocumented": 2}
    for route in sorted(spec.routes, key=lambda r: (r["tags"][:1], r["path"], r["method"])):
        rng = random.Random(route["key"])        # stable across regenerations
        folder = (route["tags"] or ["API"])[0]
        item = request_item(route, spec, rng)
        item["_state"] = operation_coverage(route)["state"]
        states[item["_state"]] += 1
        folders.setdefault(folder, []).append(item)
    for items in folders.values():
        items.sort(key=lambda i: (order[i.pop("_state")], i["name"]))

    legend = (f"{FULL} {states['complete']} fully specified — response schema declared; "
              f"every field, type and status below is the real contract.\n"
              f"{PARTIAL} {states['partial']} partly specified — readable, but no schema to "
              f"check a response against.\n"
              f"{THIN} {states['undocumented']} response shape not declared — the mock's body "
              f"is inferred. Explore them, but do not assert on their shape yet.\n\n"
              f"Requests are sorted so the fully specified ones sit at the top of each "
              f"folder.")

    return {
        "info": {
            "name": f"{spec.title or 'API'} (mock)",
            "description":
                legend + "\n\n"
                "Generated from the OpenAPI document by postman.py.\n\n"
                "Set `baseUrl` to the mock (http://localhost:4010) or to any real "
                "environment — the requests and tests are the same either way.\n\n"
                "The X-Mock-* headers on every request are disabled by default; "
                "enable one to force a documented status, pick a scenario, null every "
                "nullable field, or slow the response down.\n\n"
                "Run it in CI with:\n"
                "  newman run <this file> --env-var baseUrl=https://api.dev.example.com",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [
            {"key": "baseUrl", "value": base_url},
            {"key": "token", "value": "", "description": "sent only if you enable the header"},
            {"key": "mockStatus", "value": "500"},
            {"key": "mockScenario", "value": "empty"},
            {"key": "mockNulls", "value": "on"},
            {"key": "mockDelay", "value": "1500"},
        ],
        "item": [{"name": name, "item": items} for name, items in sorted(folders.items())],
    }


def to_curl(method, url, headers=None, body=None, pretty=True):
    """A curl a tester can paste into a terminal or into Postman's Import > Raw text."""
    sep = " \\\n  " if pretty else " "
    parts = [f"curl -X {method} '{url}'"]
    for name, value in (headers or {}).items():
        parts.append(f"-H '{name}: {value}'")
    if body:
        text = body if isinstance(body, str) else json.dumps(body, indent=2)
        parts.append("-d '" + text.replace("'", "'\\''") + "'")
    return sep.join(parts)


def main():
    ap = argparse.ArgumentParser(description="Generate a Postman collection from a spec")
    ap.add_argument("--spec", default=None,
                    help="Path or http(s) URL of the spec "
                         "(default: the project spec — see project.py)")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--env", help="Named environment from environments.json: sets baseUrl "
                                  "and writes a matching Postman environment file")
    ap.add_argument("--emit-environment", metavar="FILE",
                    help="Also write a Postman environment (secrets left blank)")
    ap.add_argument("--out", default="mockd.postman_collection.json")
    ap.add_argument("--header", action="append", default=[], metavar="'K: V'",
                    help="Header used when fetching a spec URL, repeatable")
    args = ap.parse_args()

    headers = {}
    for raw in args.header:
        name, _, value = raw.partition(":")
        headers[name.strip()] = value.strip()

    base_url = args.base_url
    env_name = args.env
    if env_name:
        import environments
        env = environments.get(env_name)
        base_url = base_url or environments.resolve(env.get("base_url", ""), [])
    base_url = base_url or "http://localhost:4010"

    # no --spec given: use the document this project is about
    args.spec, spec_from = project.resolve(args.spec, "postman")
    src = Source(args.spec, headers=headers, poll=0, cache_dir="logs")
    text, _ = src.read(force=True)
    if text is None:
        raise SystemExit(f"could not read spec from {args.spec}: {src.error}")
    spec = Spec(text=text, origin=args.spec)

    collection = build(spec, base_url)
    Path(args.out).write_text(json.dumps(collection, indent=2, ensure_ascii=False) + "\n")
    requests = sum(len(f["item"]) for f in collection["item"])
    print(f"wrote {args.out}: {requests} requests in {len(collection['item'])} folders, "
          f"baseUrl={base_url}")
    if args.emit_environment or env_name:
        out = args.emit_environment or f"{env_name}.postman_environment.json"
        # secrets stay blank on purpose: this file gets shared, the token does not
        env_doc = {
            "name": env_name or "mock",
            "values": [
                {"key": "baseUrl", "value": base_url, "enabled": True, "type": "default"},
                {"key": "token", "value": "", "enabled": True, "type": "secret"},
                {"key": "mockStatus", "value": "500", "enabled": True, "type": "default"},
                {"key": "mockScenario", "value": "empty", "enabled": True, "type": "default"},
                {"key": "mockNulls", "value": "on", "enabled": True, "type": "default"},
                {"key": "mockDelay", "value": "1500", "enabled": True, "type": "default"},
            ],
            "_postman_variable_scope": "environment",
        }
        Path(out).write_text(json.dumps(env_doc, indent=2) + "\n")
        print(f"wrote {out}: Postman environment — `token` is intentionally empty, "
              f"fill it in Postman so it never reaches git")
    print("  Postman: Import > File, then pick this file")
    print(f"  CI:      newman run {args.out} --env-var baseUrl=<environment>")


if __name__ == "__main__":
    main()
