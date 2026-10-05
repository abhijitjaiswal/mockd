#!/usr/bin/env python3
"""
observe.py — what real traffic says that the document does not.

Given exchanges with a real API — recorded by recorder.py, or exported from a
browser as a .har file — and the API document, this works out, unasked:

  * endpoints the server answers that the document does not have
  * statuses it returns that the document does not mention
  * fields that differ from the document: missing, or of another type
  * fields it returns that the document leaves out
  * which documented endpoints were actually used, and which never were

and turns the sequence that was recorded into a test: the same calls in the
same order, with each id taken from the response that produced it rather than
copied, so the test runs again tomorrow and on another server.

    python observe.py                          # what the last recording found
    python observe.py --har session.har        # the same, from a browser export
    python observe.py --make-tests my-flow     # save the recording as a test
"""
import argparse
import base64
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

HERE = Path(__file__).resolve().parent
RECORDED = HERE / "logs" / "recorded.jsonl"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# ------------------------------------------------------------------- loading
def load(path=RECORDED):
    out = []
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _as_json(text):
    try:
        return json.loads(text) if text and text.strip()[:1] in "{[" else None
    except ValueError:
        return None


def from_har(text, spec=None):
    """Exchanges from a browser's "Save all as HAR".

    A page talks to many hosts — its own API, fonts, analytics. Which one is
    the API is worked out, not asked: the host whose paths match the document
    most often. Everything else in the file is ignored."""
    try:
        entries = (json.loads(text).get("log") or {}).get("entries") or []
    except (ValueError, AttributeError):
        raise ValueError("that is not a .har file")
    parsed = []
    for entry in entries:
        request, response = entry.get("request") or {}, entry.get("response") or {}
        url = urlparse(request.get("url") or "")
        if not url.path:
            continue
        content = response.get("content") or {}
        body = content.get("text") or ""
        if content.get("encoding") == "base64":
            try:
                body = base64.b64decode(body).decode("utf-8", "replace")
            except Exception:
                body = ""
        mime = (content.get("mimeType") or "").split(";")[0].strip()
        parsed.append((url.netloc, {
            "at": entry.get("startedDateTime") or "",
            "method": (request.get("method") or "GET").upper(), "path": url.path,
            "query": dict(parse_qsl(url.query)),
            "request_body": _as_json(((request.get("postData") or {}).get("text")) or ""),
            "status": int(response.get("status") or 0), "content_type": mime,
            "response": _as_json(body) if "json" in mime or body[:1] in ("{", "[") else None,
            "bytes": len(body), "ms": int(entry.get("time") or 0)}))

    hosts = {}
    for host, exchange in parsed:
        score = hosts.setdefault(host, {"match": 0, "json": 0})
        if spec is not None and spec.match(exchange["method"], exchange["path"])[0] is not None:
            score["match"] += 1
        if "json" in exchange["content_type"]:
            score["json"] += 1
    if not hosts:
        return [], None
    best = max(hosts, key=lambda h: (hosts[h]["match"], hosts[h]["json"]))
    if spec is not None and not hosts[best]["match"]:
        return [], None                       # nothing in it is this API
    kept = [e for host, e in parsed if host == best
            and e["method"] != "OPTIONS" and e["status"]
            and ("json" in e["content_type"] or e["status"] in (204, 304)
                 or (spec is not None and spec.match(e["method"], e["path"])[0] is not None))]
    return kept, best


# ------------------------------------------------------------------ analysis
def _schema_for(route, status):
    resp = route["responses"].get(str(status)) or {}
    return ((resp.get("content") or {}).get("application/json") or {}).get("schema")


def _branch(schema, value):
    """The branch of an anyOf/oneOf that this value is an instance of."""
    for comb in ("anyOf", "oneOf"):
        if isinstance(schema, dict) and schema.get(comb):
            for option in schema[comb]:
                if not isinstance(option, dict) or option.get("type") == "null":
                    continue
                kind = option.get("type") or ("object" if "properties" in option else None)
                if (kind == "object" and isinstance(value, dict)) \
                        or (kind == "array" and isinstance(value, list)):
                    return _branch(option, value)
            return {}
    return schema if isinstance(schema, dict) else {}


def extra_fields(schema, value, prefix="", depth=0, out=None):
    """Fields the answer carries that the document does not mention."""
    out = [] if out is None else out
    if depth > 8:
        return out
    schema = _branch(schema, value)
    if isinstance(value, dict):
        props = schema.get("properties")
        if isinstance(props, dict) and props and not isinstance(schema.get("additionalProperties"), dict):
            for key in value:
                if key not in props:
                    out.append(f"{prefix}{key}")
        for key, sub in (props or {}).items():
            if isinstance(value.get(key), (dict, list)):
                extra_fields(sub, value[key], f"{prefix}{key}.", depth + 1, out)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for item in value[:3]:
            extra_fields(schema["items"], item, f"{prefix}[]." if not prefix.endswith("[].")
                         else prefix, depth + 1, out)
    return out


def _tidy(path):
    return re.sub(r"\.?\[\]\.", "[].", path).strip(".")


def analyse(exchanges, spec):
    """Everything the traffic shows, set against the document."""
    try:
        from jsonschema import Draft202012Validator as Validator
    except ImportError:                                   # pragma: no cover
        from jsonschema import Draft7Validator as Validator

    used, unknown, statuses, shape = {}, {}, {}, {}
    for exchange in exchanges:
        method, path, status = exchange.get("method"), exchange.get("path"), exchange.get("status")
        route, _ = spec.match(method, path)
        if route is None:
            # a page, a stylesheet or the document itself is not an API endpoint
            if "json" not in str(exchange.get("content_type") or "") and status not in (204,) \
                    or re.search(r"(openapi|swagger)[^/]*\.(json|ya?ml)$|/api-docs$", path or "", re.I):
                continue
            # one line per endpoint, not per id: /orders/123 and /orders/456 are one
            generic = re.sub(r"/(\d+|[0-9a-f]{8}-[0-9a-f-]{27,})(?=/|$)", "/{id}", path, flags=re.I)
            slot = unknown.setdefault((method, generic), {"calls": 0, "statuses": set()})
            slot["calls"] += 1
            slot["statuses"].add(status)
            continue
        key = route["key"]
        slot = used.setdefault(key, {"calls": 0, "statuses": {}})
        slot["calls"] += 1
        slot["statuses"][status] = slot["statuses"].get(status, 0) + 1

        documented = {str(c) for c in route["responses"]}
        if str(status) not in documented and "default" not in documented:
            entry = statuses.setdefault((key, status), {"calls": 0})
            entry["calls"] += 1
            continue
        schema, body = _schema_for(route, status), exchange.get("response")
        if not schema or body is None:
            continue
        for error in list(Validator(schema).iter_errors(body))[:20]:
            where = ".".join("[]" if isinstance(p, int) else str(p) for p in error.absolute_path)
            if error.validator == "required":
                missing = re.findall(r"'([^']+)' is a required property", error.message)
                where, kind = _tidy(f"{where}.{missing[0]}" if missing else where), "missing"
                detail = "the document says it is always there; it was not"
            elif error.validator == "type":
                kind, where = "type", _tidy(where)
                detail = f"the document says {error.validator_value}; it was {type(error.instance).__name__}"
            else:
                kind, where = "differs", _tidy(where)
                detail = error.message[:140]
            entry = shape.setdefault((key, status, kind, where), {"calls": 0, "detail": detail})
            entry["calls"] += 1
        for field in {_tidy(f) for f in extra_fields(schema, body)}:
            entry = shape.setdefault((key, status, "extra", field),
                                     {"calls": 0, "detail": "returned, but not in the document"})
            entry["calls"] += 1

    documented_ops = [r["key"] for r in spec.routes]
    found = {
        "calls": len(exchanges),
        "used": [{"operation": k, "calls": v["calls"],
                  "statuses": {str(s): n for s, n in sorted(v["statuses"].items())}}
                 for k, v in sorted(used.items(), key=lambda kv: -kv[1]["calls"])],
        "unused": [k for k in documented_ops if k not in used],
        "documented": len(documented_ops),
        "undocumented_endpoints": [
            {"method": m, "path": p, "calls": v["calls"], "statuses": sorted(v["statuses"])}
            for (m, p), v in sorted(unknown.items(), key=lambda kv: -kv[1]["calls"])],
        "undocumented_statuses": [
            {"operation": k, "status": s, "calls": v["calls"]}
            for (k, s), v in sorted(statuses.items())],
        "fields": [{"operation": k, "status": s, "kind": kind, "field": field,
                    "detail": v["detail"], "calls": v["calls"]}
                   for (k, s, kind, field), v in sorted(shape.items(), key=lambda kv: (
                       {"missing": 0, "type": 1, "differs": 2, "extra": 3}[kv[0][2]], kv[0][0]))],
    }
    found["disagreements"] = (len(found["undocumented_endpoints"])
                              + len(found["undocumented_statuses"]) + len(found["fields"]))
    found["sentence"] = sentence(found)
    return found


def _count(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def sentence(found):
    if not found["calls"]:
        return "Nothing has been recorded yet."
    wrong = [f for f in found["fields"] if f["kind"] != "extra"]
    extra = [f for f in found["fields"] if f["kind"] == "extra"]
    parts = []
    if found["undocumented_endpoints"]:
        parts.append(_count(len(found["undocumented_endpoints"]), "endpoint") + " the document does not have")
    if found["undocumented_statuses"]:
        parts.append(_count(len(found["undocumented_statuses"]), "status", "statuses") + " it does not mention")
    if wrong:
        parts.append(_count(len(wrong), "field") + " that differ" + ("s" if len(wrong) == 1 else "") + " from it")
    if extra:
        parts.append(_count(len(extra), "field") + " it leaves out")
    head = f"Across {_count(found['calls'], 'call')}: "
    body = (", ".join(parts) + ".") if parts else "the real API did exactly what the document says."
    return (head + body + f" {len(found['used'])} of {found['documented']} documented "
            f"endpoints were used.")


# ------------------------------------------------------- a recording as a test
def _ids_in(value, path="", out=None, depth=0):
    """Values in an answer that look like ids, with where they sit."""
    out = {} if out is None else out
    if depth > 5:
        return out
    if isinstance(value, dict):
        for key, sub in value.items():
            where = f"{path}.{key}" if path else key
            low = str(key).lower()
            if isinstance(sub, (str, int)) and not isinstance(sub, bool) \
                    and (low == "id" or low.endswith("_id") or low.endswith("id")
                         or (isinstance(sub, str) and UUID.match(sub))):
                out.setdefault(str(sub), where)
            elif isinstance(sub, (dict, list)):
                _ids_in(sub, where, out, depth + 1)
    elif isinstance(value, list) and value:
        _ids_in(value[0], f"{path}[0]", out, depth + 1)
    return out


def _camel(name):
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", str(name)) if p]
    return (parts[0].lower() + "".join(p[:1].upper() + p[1:] for p in parts[1:])) if parts else "value"


def _singular(word):
    word = str(word)
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _swap(node, known, need):
    """Replace every known id inside a request body with its variable."""
    if isinstance(node, dict):
        return {k: _swap(v, known, need) for k, v in node.items()}
    if isinstance(node, list):
        return [_swap(v, known, need) for v in node]
    if isinstance(node, (str, int)) and not isinstance(node, bool) and str(node) in known:
        need.add(str(node))
        return "{{%s}}" % known[str(node)]["var"]
    return node


def test_from(exchanges, spec, name="recorded", server=None, limit=15):
    """The recorded calls as one flow that can be run again.

    An id is never copied from the recording: where a request used a value an
    earlier answer contained, that earlier step captures it and this one uses
    the variable. An id that came from nowhere in the recording — the first
    thing asked for — is kept as the test's own data, true for that server."""
    steps, known, notes, data = [], {}, [], {}
    last = None
    for exchange in exchanges:
        route, params = spec.match(exchange.get("method"), exchange.get("path"))
        if route is None or not (200 <= int(exchange.get("status") or 0) < 300):
            continue
        signature = (exchange["method"], exchange["path"], json.dumps(exchange.get("query"), sort_keys=True),
                     json.dumps(exchange.get("request_body"), sort_keys=True))
        if signature == last:
            continue                               # the page asked twice; once is the test
        last = signature
        if len(steps) >= limit:
            notes.append(f"only the first {limit} calls were kept")
            break

        need = set()
        path = route["path"]
        for param, value in (params or {}).items():
            value = str(value)
            if value in known:
                need.add(value)
                path = path.replace("{%s}" % param, "{{%s}}" % known[value]["var"])
            else:
                var = "known" + _camel(param)[:1].upper() + _camel(param)[1:]
                data[var] = int(value) if value.isdigit() else value
                path = path.replace("{%s}" % param, "{{%s}}" % var)
        body = _swap(exchange.get("request_body"), known, need)
        for value in need:                          # the step that produced it captures it
            source = known[value]
            steps[source["step"]].setdefault("capture", {})[source["var"]] = source["path"]

        request = {"method": exchange["method"], "path": path}
        if exchange.get("query"):
            request["query"] = exchange["query"]
        if body is not None and exchange["method"] in ("POST", "PUT", "PATCH"):
            request["body"] = body
        # The test holds the API to the document, not to what happened to be
        # recorded. A 200 where the document says 201 is a finding, and a test
        # that expected the 200 would go on passing while the bug stayed.
        documented = sorted(int(c) for c in route["responses"] if str(c).isdigit() and str(c).startswith("2"))
        if str(exchange["status"]) in {str(c) for c in route["responses"]} or not documented:
            assertions = [{"type": "status", "equals": exchange["status"]}]
        else:
            assertions = [{"type": "status", "in": documented}]
            notes.append(f"{route['key']} answered {exchange['status']} in the recording, which the "
                         f"document does not mention; the test expects "
                         f"{' or '.join(str(c) for c in documented)}, as documented")
        if _schema_for(route, exchange["status"]) or _schema_for(route, (documented or [0])[0]):
            assertions.append({"type": "schema"})
        steps.append({"role": "step", "name": route.get("summary") or route["key"],
                      "request": request, "assertions": assertions})

        named = [part for part in route["path"].split("/") if part and "{" not in part]
        resource = _singular(named[-1]) if named else "item"
        for value, where in _ids_in(exchange.get("response")).items():
            if value in known:
                continue
            leaf = where.split(".")[-1]
            var = _camel(f"{resource}_id" if leaf.lower() == "id" else leaf)
            taken = {k["var"] for k in known.values()}
            base, n = var, 2
            while var in taken:
                var, n = f"{base}{n}", n + 1
            known[value] = {"var": var, "path": where, "step": len(steps) - 1}

    if not steps:
        return None, ["nothing in the recording is a successful call to a documented endpoint"]
    steps[-1]["role"] = "target"
    if data:
        notes.append("ids that came from outside the recording are kept as this test's data: "
                     + ", ".join(sorted(data)) + " — true for the server it was recorded on")
    test = {"id": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "recorded",
            "name": f"What was recorded{' on ' + server if server else ''}: "
                    f"{steps[0]['name']} … {steps[-1]['name']}" if len(steps) > 1
                    else f"What was recorded: {steps[0]['name']}",
            "kind": "e2e", "levels": ["regression"], "priority": "P2",
            "description": "The calls made while traffic was being watched, in the same order, "
                           "with each id taken from the answer that produced it.",
            "tags": ["recorded"], "steps": steps}
    if data:
        test["data"] = data
    return test, notes


def main():
    ap = argparse.ArgumentParser(description="What real traffic says that the document does not")
    ap.add_argument("--recording", default=str(RECORDED))
    ap.add_argument("--har", help="a .har file exported from a browser, instead")
    ap.add_argument("--spec", help="the API document (default: the project's)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--make-tests", metavar="MODULE", help="save the recording as a test in this module")
    args = ap.parse_args()

    sys.path.insert(0, str(HERE))
    import project
    from mockd import Source, Spec
    path = args.spec or project.active_spec()
    text, _ = Source(path, poll=0, cache_dir=str(HERE / "logs")).read(force=True)
    spec = Spec(text=text, origin=path)
    if args.har:
        exchanges, host = from_har(Path(args.har).read_text(), spec)
        if host:
            print(f"the API in that file is {host}", file=sys.stderr)
    else:
        exchanges = load(args.recording)

    if args.make_tests:
        import tests as t
        test, notes = test_from(exchanges, spec, name=args.make_tests)
        if test is None:
            print(notes[0])
            return 1
        written = t.import_tests([test], args.make_tests, stage="draft")
        print("saved" if written.get("ok") else "not saved: " + "; ".join(written.get("errors") or []))
        for note in notes:
            print(" ", note)
        return 0 if written.get("ok") else 1

    found = analyse(exchanges, spec)
    if args.json:
        print(json.dumps(found, indent=2))
        return 0
    print(found["sentence"])
    for item in found["undocumented_endpoints"]:
        print(f"  not in the document   {item['method']} {item['path']}  ({item['calls']} calls)")
    for item in found["undocumented_statuses"]:
        print(f"  status not mentioned  {item['operation']} answered {item['status']}")
    for item in found["fields"]:
        print(f"  {item['kind']:<20}  {item['operation']} {item['status']}: {item['field']} — {item['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
