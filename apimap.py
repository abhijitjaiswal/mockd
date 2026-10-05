#!/usr/bin/env python3
"""
apimap.py — the whole API on one screen: every endpoint, and what is known about it.

Everything mockd works out ends up in a list somewhere: a failing test, an
endpoint nobody tests, one that answers without signing in, one that got
slower, one whose answers differ from the document. Each is true and each is
in a different place. This puts them on the endpoints they are about, grouped
by resource, for one server at a time — the data behind the map on Home.

An endpoint's state comes from the last time each test that touches it ran on
that server, step by step, so a flow that fails at its third call marks the
third call and not the two before it.
"""
import glob
import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _resource(route):
    import blueprint
    key = blueprint.family_key(route["path"])
    return (key[0] if key else "other").replace("_", "-")


def _latest_steps(spec, env, runs_dir, limit=150):
    """{operation: {"pass": n, "fail": n}} from each test's most recent run on `env`."""
    seen, out = set(), {}
    files = sorted(glob.glob(str(Path(runs_dir) / "run-*.json")), key=os.path.getmtime,
                   reverse=True)[:limit]
    for name in files:
        try:
            report = json.loads(Path(name).read_text())
        except (OSError, ValueError):
            continue
        if (report.get("env") or report.get("base_url")) != env:
            continue
        for suite in report.get("suites") or []:
            for item in suite.get("results") or []:
                test = (suite.get("name"), item.get("id"))
                if test in seen:
                    continue                       # an older run of a test already counted
                seen.add(test)
                for step in item.get("steps") or []:
                    request = step.get("request") or {}
                    if step.get("status") is None:
                        continue                   # never sent: blocked, or refused
                    path = re.sub(r"^https?://[^/]+", "", str(request.get("url") or "")).split("?")[0]
                    route, _ = spec.match(str(request.get("method") or "GET").upper(), path)
                    if route is None:
                        continue
                    slot = out.setdefault(route["key"], {"pass": 0, "fail": 0, "open": False})
                    if step.get("outcome") == "pass":
                        slot["pass"] += 1
                    else:
                        slot["fail"] += 1
                        if "access" in (item.get("tags") or []) and 200 <= int(step["status"]) < 300:
                            slot["open"] = True
    return out


def build(spec, suites, env="mock", runs_dir=None, slow=None, differs=None, used=None):
    """The map for one server.

    `slow`, `differs` and `used` are sets of operation keys from perf.py and
    observe.py; they are passed in so this stays a pure join."""
    import impact
    runs_dir = runs_dir or HERE / "logs"
    slow, differs, used = set(slow or ()), set(differs or ()), set(used or ())

    touching = {}
    for suite in suites:
        for test in (suite.get("scenarios") or []) + (suite.get("cases") or []):
            key = f"{suite.get('name')}|{suite.get('_stage', 'shared')}|{test.get('id')}"
            for operation in impact.operations_of(test, spec):
                touching.setdefault(operation, []).append(key)

    ran = _latest_steps(spec, env, runs_dir)
    groups, counts = {}, {"pass": 0, "fail": 0, "idle": 0, "none": 0, "open": 0}
    for route in spec.routes:
        key = route["key"]
        seen = ran.get(key)
        state = ("fail" if seen and seen["fail"] else "pass" if seen and seen["pass"]
                 else "idle" if touching.get(key) else "none")
        counts[state] += 1
        if seen and seen["open"]:
            counts["open"] += 1
        groups.setdefault(_resource(route), []).append({
            "key": key, "method": route["method"], "path": route["path"],
            "summary": route.get("summary") or "", "state": state,
            "tests": touching.get(key, []),
            "open": bool(seen and seen["open"]),
            "slow": key in slow, "differs": key in differs, "used": key in used})

    order = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}
    resources = [{"name": name,
                  "endpoints": sorted(items, key=lambda e: (e["path"].count("/"), e["path"],
                                                            order.get(e["method"], 9)))}
                 for name, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    total = len(spec.routes)
    return {"env": env, "total": total, "counts": counts,
            "health": round(100 * counts["pass"] / total) if total else 0,
            "resources": resources,
            "sentence": sentence(env, total, counts)}


def sentence(env, total, counts):
    where = "the mock" if env == "mock" else env
    if not total:
        return "There is no API document yet."
    if not (counts["pass"] or counts["fail"]):
        return f"Nothing has been run on {where} yet."
    parts = [f"{counts['pass']} of {total} endpoints proven on {where}"]
    if counts["fail"]:
        parts.append(f"{counts['fail']} failing")
    if counts["open"]:
        parts.append(f"{counts['open']} open to anyone")
    untested = counts["none"]
    if untested:
        parts.append(f"{untested} with no test")
    return " · ".join(parts) + "."
