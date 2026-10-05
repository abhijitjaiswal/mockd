#!/usr/bin/env python3
"""
perf.py — how fast the API answers, noticed and measured.

Two things, both from tests that already exist.

Noticed. Every run already records how long each call took, and nobody looks.
`findings()` reads those runs back and says, per server, which endpoints are
slow and — more useful — which have become slower than they themselves used to
be. Nothing to set up; it is there the day after the tests are.

Measured. `load()` takes tests that only read and runs them over and over with
several callers at once for a set time, then gives each endpoint's median and
its 95th and 99th percentile, the error count and the calls per second. A
budget — "95% of calls within 500 ms" — makes it a gate for CI.

    python perf.py                                   # what the runs so far show
    python perf.py run --env dev --users 5 --seconds 20
    python perf.py run --env dev --p95 500           # exit 1 if any endpoint is over

It is for catching a regression and the obviously slow. It is not a substitute
for a load-testing tool when the question is thousands of users. It only ever
reads unless told otherwise, and a server marked read only is never written to.
"""
import argparse
import glob
import json
import os
import re
import statistics
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SLOW_MS = 1000              # an endpoint whose usual answer takes longer is called slow
SLOWER_BY = 2.0             # ...and slower when it takes this many times what it used to
SLOWER_AT_LEAST = 150       # and by at least this many milliseconds, so 4 ms -> 9 ms is not news
MAX_USERS, MAX_SECONDS = 50, 300


def _spec():
    import project
    from mockd import Source, Spec
    path = project.active_spec()
    text, _ = Source(path, poll=0, cache_dir=str(HERE / "logs")).read(force=True)
    return Spec(text=text, origin=path) if text else None


def _operation(spec, method, url):
    path = re.sub(r"^https?://[^/]+", "", str(url or "")).split("?")[0]
    route, _ = spec.match(str(method or "GET").upper(), path) if spec is not None else (None, None)
    return route["key"] if route is not None else None


# ------------------------------------------------------------------- noticed
def timings(spec, runs_dir=None, limit=150):
    """{server: {operation: [ms, …oldest first]}} from the saved runs."""
    out = {}
    files = sorted(glob.glob(str(Path(runs_dir or HERE / "logs") / "run-*.json")),
                   key=os.path.getmtime)[-limit:]
    for name in files:
        try:
            report = json.loads(Path(name).read_text())
        except (OSError, ValueError):
            continue
        server = report.get("env") or report.get("base_url") or ""
        if not server or re.match(r"https?:", server):
            continue                                   # a run nobody named a server for
        for suite in report.get("suites") or []:
            for item in suite.get("results") or []:
                for step in item.get("steps") or []:
                    request = step.get("request") or {}
                    if not step.get("status") or not step.get("ms"):
                        continue
                    key = _operation(spec, request.get("method"), request.get("url"))
                    if key:
                        out.setdefault(server, {}).setdefault(key, []).append(int(step["ms"]))
    return out


def findings(spec=None, runs_dir=None):
    """Per server: endpoints that are slow, and ones that got slower."""
    spec = spec or _spec()
    found = {}
    for server, operations in timings(spec, runs_dir).items():
        if server.startswith("mock"):
            continue                                   # the mock's speed is nobody's news
        slow, slower = [], []
        for key, samples in operations.items():
            recent = samples[-5:]
            usual = statistics.median(recent)
            if usual >= SLOW_MS:
                slow.append({"operation": key, "ms": int(usual), "calls": len(samples)})
            if len(samples) >= 6:
                now, before = statistics.median(samples[-3:]), statistics.median(samples[:-3][-10:])
                if now >= before * SLOWER_BY and now - before >= SLOWER_AT_LEAST:
                    slower.append({"operation": key, "was": int(before), "now": int(now)})
        if slow or slower:
            found[server] = {"slow": sorted(slow, key=lambda x: -x["ms"]),
                             "slower": sorted(slower, key=lambda x: x["was"] - x["now"])}
    return found


# ------------------------------------------------------------------ measured
def _percentile(values, share):
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(share * (len(ordered) - 1)))))
    return int(ordered[index])


def _only_reads(test):
    steps = (test.get("steps") or [test]) + (test.get("cleanup") or [])
    return all(str((s.get("request") or s).get("method", "GET")).upper() in ("GET", "HEAD")
               for s in steps)


def pick(suites, only=None, include_writes=False):
    """The tests worth repeating under load: ones that only read, are meant to
    succeed, and are not waiting on anything."""
    import tests as t
    chosen = []
    for suite in suites:
        for test in (suite.get("cases") or []) + (suite.get("scenarios") or []):
            key = f"{suite.get('name')}|{suite.get('_stage', 'shared')}|{test.get('id')}"
            if only is not None and key not in only:
                continue
            if t.status_of(test) != "ready" or "negative" in t.levels_of(test) \
                    or "access" in (test.get("tags") or []) or t.waiting_for(test, suite.get("data")):
                continue
            if not include_writes and not _only_reads(test):
                continue
            chosen.append((suite, test))
    return chosen


def load(env_name, users=5, seconds=20, only=None, include_writes=False, p95_budget=None,
         progress=None, stop=None):
    """Run the chosen tests repeatedly with `users` callers at once for `seconds`."""
    import environments as envmod
    import tests as t
    users = max(1, min(int(users), MAX_USERS))
    seconds = max(1, min(int(seconds), MAX_SECONDS))
    spec = _spec()
    base, headers, _ = envmod.headers_for(env_name)
    env_data = envmod.data_for(env_name)
    readonly = envmod.is_readonly(env_name)
    chosen = pick(t.load_suites(include_drafts=True), only, include_writes and not readonly)
    if not chosen:
        return {"ok": False, "error": "None of these tests only reads, so there is nothing safe "
                                      "to repeat. Tests that create or change things are left out."}
    runner = t.Runner(base, headers, None, 30, verbose=False, readonly=readonly, env_name=env_name)

    samples, lock = {}, threading.Lock()
    counts = {"calls": 0}
    deadline = time.time() + seconds
    started = time.time()

    def caller(offset):
        index = offset
        while time.time() < deadline and not (stop and stop.is_set()):
            suite, test = chosen[index % len(chosen)]
            index += 1
            data = {**t.interpolate(suite.get("data") or {}, {}, strict=False), **env_data}
            try:
                result = (runner.run_scenario(test, data) if "steps" in test
                          else runner.run_case(test, data))
            except Exception:
                continue
            with lock:
                for step in result.get("steps") or []:
                    request = step.get("request") or {}
                    key = _operation(spec, request.get("method"), request.get("url")) \
                        or f"{request.get('method')} {re.sub(r'^https?://[^/]+', '', str(request.get('url') or ''))}"
                    slot = samples.setdefault(key, {"ms": [], "errors": 0, "failed": 0})
                    status = step.get("status")
                    if status is None or int(status) >= 500:
                        slot["errors"] += 1
                    else:
                        slot["ms"].append(int(step.get("ms") or 0))
                        if step.get("outcome") not in ("pass", None):
                            slot["failed"] += 1
                    counts["calls"] += 1
            if progress:
                progress(counts["calls"], time.time() - started)

    threads = [threading.Thread(target=caller, args=(i,), daemon=True) for i in range(users)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=seconds + 40)
    took = max(time.time() - started, 0.001)

    rows = []
    for key, slot in samples.items():
        ms = slot["ms"]
        row = {"operation": key, "calls": len(ms) + slot["errors"], "errors": slot["errors"],
               "failed_checks": slot["failed"],
               "median": _percentile(ms, 0.5), "p95": _percentile(ms, 0.95),
               "p99": _percentile(ms, 0.99), "slowest": max(ms) if ms else 0}
        if p95_budget:
            row["over_budget"] = row["p95"] > int(p95_budget)
        rows.append(row)
    rows.sort(key=lambda r: -r["p95"])
    total = sum(r["calls"] for r in rows)
    errors = sum(r["errors"] for r in rows)
    over = [r for r in rows if r.get("over_budget")]
    result = {"ok": True, "server": env_name, "users": users, "seconds": round(took, 1),
              "tests": len(chosen), "calls": total, "per_second": round(total / took, 1),
              "errors": errors, "rows": rows, "p95_budget": p95_budget,
              "within_budget": (not over and not errors) if p95_budget else None,
              "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    result["sentence"] = sentence(result)
    return result


def sentence(result):
    rows = result.get("rows") or []
    if not rows:
        return "No calls were made."
    worst = rows[0]
    said = (f"{result['calls']:,} calls in {result['seconds']:g} s with {result['users']} at once — "
            f"{result['per_second']:g} a second. Slowest endpoint: {worst['operation']}, "
            f"{worst['p95']} ms for 95% of calls.")
    said += (f" {result['errors']} call{'s' if result['errors'] != 1 else ''} failed outright."
             if result["errors"] else " None failed.")
    if result.get("p95_budget"):
        over = [r for r in rows if r.get("over_budget")]
        said += (f" {len(over)} endpoint{'s are' if len(over) != 1 else ' is'} over the "
                 f"{result['p95_budget']} ms budget." if over
                 else f" Every endpoint is within the {result['p95_budget']} ms budget.")
    return said


def main():
    ap = argparse.ArgumentParser(description="How fast the API answers")
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="repeat the read-only tests under load")
    run.add_argument("--env", required=True, help="a server set up in mockd")
    run.add_argument("--users", type=int, default=5)
    run.add_argument("--seconds", type=int, default=20)
    run.add_argument("--suite", action="append", help="only these modules")
    run.add_argument("--writes", action="store_true", help="also repeat tests that write")
    run.add_argument("--p95", type=int, help="fail if any endpoint's 95th percentile is over this (ms)")
    run.add_argument("--json", help="write the full result here")
    args = ap.parse_args()

    if args.cmd != "run":
        found = findings()
        if not found:
            print("Nothing slow, and nothing slower than it was, in the runs so far.")
        for server, f in found.items():
            for item in f["slower"]:
                print(f"{server}: {item['operation']} got slower — {item['was']} ms, now {item['now']} ms")
            for item in f["slow"]:
                print(f"{server}: {item['operation']} is slow — usually {item['ms']} ms")
        return 0

    import tests as t
    only = None
    if args.suite:
        only = {f"{s.get('name')}|{s.get('_stage', 'shared')}|{x.get('id')}"
                for s in t.load_suites(include_drafts=True) if s.get("name") in args.suite
                for x in (s.get("cases") or []) + (s.get("scenarios") or [])}
    result = load(args.env, args.users, args.seconds, only, args.writes, args.p95)
    if not result.get("ok"):
        print(result.get("error"))
        return 1
    print(result["sentence"])
    print(f"\n  {'endpoint':<52} {'calls':>6} {'median':>7} {'p95':>6} {'p99':>6} {'errors':>6}")
    for row in result["rows"]:
        print(f"  {row['operation'][:52]:<52} {row['calls']:>6} {row['median']:>7} {row['p95']:>6} "
              f"{row['p99']:>6} {row['errors']:>6}" + ("   OVER" if row.get("over_budget") else ""))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1))
    return 0 if result.get("within_budget") in (None, True) else 1


if __name__ == "__main__":
    sys.exit(main())
