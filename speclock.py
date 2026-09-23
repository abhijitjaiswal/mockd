#!/usr/bin/env python3
"""
speclock.py — identify the spec, don't just load it.

The problem this exists for: a green test run means nothing unless you know
which document it was green against. The spec is an INPUT that decides whether
tests pass, so anyone who can quietly swap it can quietly change the result —
including swapping in yesterday's export so a regression stops showing.

You cannot stop someone opening an old file on their own laptop, and you should
not try: comparing old against new is a legitimate and useful thing to do. What
you can do is make it impossible to do SILENTLY, and impossible to land in CI:

  1. Every spec load is content-addressed (sha256). Nothing is "the spec"; it is
     always "the spec with digest abc123".
  2. `spec.lock.json` is committed. It records the digest, where it came from,
     when, and how many operations it had. Changing specs changes the lockfile,
     which shows up as a reviewable diff — the same trick package-lock.json uses.
  3. CI re-fetches from the recorded source and refuses to run if the digest
     moved. Upstream changing is fine; it just has to be re-locked deliberately.
  4. Every test run, verify run and overlay build is stamped with the digest it
     used, so a result can always be traced to a document.
  5. An upload or a URL change is a CANDIDATE, not an adoption. The console shows
     the difference and an explicit action rewrites the lock.

    python speclock.py show
    python speclock.py lock --spec apis.json --note "release 2.4"
    python speclock.py check --spec apis.json          # CI gate
    python speclock.py diff --spec new_apis.json       # what changed
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK = HERE / "spec.lock.json"


def digest(text):
    """Content address. Normalised so a re-export with different whitespace or
    key order is not mistaken for a real change."""
    try:
        parsed = json.loads(text)
        canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    except ValueError:
        canonical = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return hashlib.sha256(canonical.encode()).hexdigest()


def summarise(text):
    """The few facts worth recording beside the digest, so a diff is readable."""
    try:
        doc = json.loads(text)
    except ValueError:
        import yaml
        doc = yaml.safe_load(text)
    paths = doc.get("paths") or {}
    methods = ("get", "post", "put", "patch", "delete", "head", "options")
    ops = sorted(f"{m.upper()} {p}"
                 for p, item in paths.items()
                 for m in item if m.lower() in methods)
    return {
        "title": (doc.get("info") or {}).get("title", ""),
        "version": (doc.get("info") or {}).get("version", ""),
        "openapi": doc.get("openapi", ""),
        "operations": len(ops),
        "schemas": len((doc.get("components") or {}).get("schemas") or {}),
        "operation_ids": ops,
    }


def load():
    if LOCK.exists():
        try:
            return json.loads(LOCK.read_text())
        except ValueError:
            return None
    return None


def write(text, source, note=None, actor=None):
    info = summarise(text)
    lock = {
        "digest": digest(text),
        "source": str(source),
        "locked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # Never harvested from the machine: this file is committed, and whose
        # laptop ran the command is not provenance anyone asked for. Teams that
        # want attribution pass --by explicitly.
        "locked_by": actor or "",
        "note": note or "",
        "title": info["title"],
        "spec_version": info["version"],
        "openapi": info["openapi"],
        "operations": info["operations"],
        "schemas": info["schemas"],
        "_why": "Committed on purpose. If this file changes in a pull request, the "
                "document the tests are judged against changed — review it like code.",
    }
    LOCK.write_text(json.dumps(lock, indent=2) + "\n")
    return lock


def compare(text, lock=None):
    """Where does this document stand relative to the lock?"""
    lock = lock or load()
    now = digest(text)
    if lock is None:
        return {"state": "unlocked", "digest": now,
                "message": "No spec.lock.json. Run `python speclock.py lock` so results "
                           "can be traced to a document."}
    if lock.get("digest") == now:
        return {"state": "match", "digest": now, "locked_at": lock.get("locked_at"),
                "message": f"Matches the lock ({now[:12]}…), locked "
                           f"{lock.get('locked_at')}"
                           + (f" by {lock['locked_by']}." if lock.get("locked_by")
                              else ".")}

    info = summarise(text)
    added = removed = None
    if lock.get("operations") is not None:
        delta = info["operations"] - lock["operations"]
        added = max(delta, 0)
        removed = max(-delta, 0)
    older = (lock.get("operations") or 0) > info["operations"]
    return {
        "state": "drift",
        "digest": now,
        "locked_digest": lock.get("digest"),
        "locked_at": lock.get("locked_at"),
        "locked_operations": lock.get("operations"),
        "operations": info["operations"],
        "added": added, "removed": removed,
        "looks_older": older,
        "message": (f"Does NOT match the lock. Loaded {info['operations']} operation(s); "
                    f"the lock records {lock.get('operations')}."
                    + (" This document has FEWER operations than the locked one, which is "
                       "what an older export looks like." if older else "")),
    }


def diff(text, other_text):
    """Operation-level diff between two documents."""
    a, b = summarise(other_text), summarise(text)
    left, right = set(a["operation_ids"]), set(b["operation_ids"])
    return {
        "from": {"title": a["title"], "version": a["version"], "operations": a["operations"]},
        "to": {"title": b["title"], "version": b["version"], "operations": b["operations"]},
        "added": sorted(right - left),
        "removed": sorted(left - right),
        "common": len(left & right),
    }


# ----------------------------------------------------------------------------


def _read(source):
    from mockd import Source
    src = Source(source, poll=0, cache_dir=str(HERE / "logs"))
    text, _ = src.read(force=True)
    if text is None:
        raise SystemExit(f"could not read {source}: {src.error}")
    return text


def main():
    ap = argparse.ArgumentParser(description="Identify and pin the spec")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("show", help="print the current lock")

    lk = sub.add_parser("lock", help="pin the current spec — commit the result")
    lk.add_argument("--spec", default="apis.json")
    lk.add_argument("--note", help="why this version, e.g. 'release 2.4'")
    lk.add_argument("--by", default=None,
                    help="who pinned it, if your team wants that recorded. Left out, "
                         "the lock names nobody — the machine's username is never used.")

    ck = sub.add_parser("check", help="CI gate: fail if the spec is not the locked one")
    ck.add_argument("--spec", default=None,
                    help="default: re-fetch from the source recorded in the lock")

    df = sub.add_parser("diff", help="operation-level diff against the locked source")
    df.add_argument("--spec", required=True)
    df.add_argument("--against", default=None, help="default: the lock's own source")
    args = ap.parse_args()

    if args.command == "show":
        lock = load()
        if not lock:
            print("no spec.lock.json — nothing is pinned")
            return 1
        print(json.dumps(lock, indent=2))
        return 0

    if args.command == "lock":
        lock = write(_read(args.spec), args.spec, args.note, args.by)
        print(f"locked {lock['title']} {lock['spec_version']} "
              f"({lock['operations']} operations) at {lock['digest'][:12]}…")
        print(f"  source: {lock['source']}")
        print(f"  commit {LOCK.name} — a change to it is a change to what the tests mean")
        return 0

    if args.command == "check":
        lock = load()
        if not lock:
            print("no spec.lock.json — run `python speclock.py lock` first")
            return 1
        source = args.spec or lock["source"]
        outcome = compare(_read(source), lock)
        print(f"spec: {source}")
        print(f"  {outcome['message']}")
        if outcome["state"] == "match":
            return 0
        print("\n  The document the tests are judged against is not the one that was")
        print("  pinned. Either the upstream spec moved — in which case re-lock on")
        print("  purpose and commit it — or something older has been substituted.")
        print(f"\n    python speclock.py diff --spec {source}")
        print(f"    python speclock.py lock --spec {source} --note 'why'")
        return 1

    if args.command == "diff":
        lock = load()
        against = args.against or (lock or {}).get("source")
        if not against:
            raise SystemExit("nothing to diff against — pass --against or create a lock")
        out = diff(_read(args.spec), _read(against))
        print(f"from {against}: {out['from']['operations']} operation(s)")
        print(f"to   {args.spec}: {out['to']['operations']} operation(s)")
        print(f"  {out['common']} unchanged, {len(out['added'])} added, "
              f"{len(out['removed'])} removed")
        for key, mark in (("added", "+"), ("removed", "-")):
            for op in out[key]:
                print(f"  {mark} {op}")
        # Removing operations is the signature of an older export being loaded.
        if out["removed"] and not out["added"]:
            print("\n  Only removals. That is what loading an OLDER export looks like.")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
