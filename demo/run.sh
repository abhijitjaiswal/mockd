#!/bin/bash
# Which browser suites to run, by name — because running all of them to check
# one change costs minutes and buys nothing.
#
#   ./demo/run.sh tests      just the Tests module — while working on it
#   ./demo/run.sh spec       just the spec / source / environment side
#   ./demo/run.sh sanity     the whole-app suites, as a check before committing
#   ./demo/run.sh all        the lot
#
# A suite named directly also works:  ./demo/run.sh workbench oppicker
cd "$(dirname "$0")/.."

# Playwright may be installed under demo/, at the repo root, or wherever the
# caller already has it. Say which, or say plainly that it is missing —
# "0 suites ran" reported as success is worse than an error.
if [ -z "$NODE_PATH" ]; then
  for candidate in "$PWD/demo/node_modules" "$PWD/node_modules"; do
    [ -d "$candidate/playwright" ] && export NODE_PATH="$candidate" && break
  done
fi
if ! NODE_PATH="$NODE_PATH" node -e "require('playwright')" 2>/dev/null; then
  echo "  playwright is not installed — run:  python bootstrap.py --with-demo"
  echo "  (or set NODE_PATH to a node_modules that has it)"
  exit 1
fi

SANITY="e2e toast"
TESTS="workbench wbasserts oppicker selectors pipeline library openload scoped envalign visible report assert runnow chain"
SPEC="specpick projectspec compare discover params newenv envlogin"
MISC="picker progress splice envvars reveal"

case "${1:-sanity}" in
  sanity) SUITES="$SANITY" ;;
  # just the module being worked on. Pulling the whole-app suites in here made
  # a change to one panel cost four minutes, so they are their own group and
  # run before committing rather than after every edit.
  tests)  SUITES="$TESTS" ;;
  spec)   SUITES="$SPEC" ;;
  all)    SUITES="$SANITY $TESTS $SPEC $MISC" ;;
  *)      SUITES="$*" ;;
esac

# a dead mock makes every suite fail for the same uninteresting reason
if ! curl -sf "localhost:4010/_mock/routes" >/dev/null 2>&1; then
  echo "  the mock is not answering on :4010 — start it from the console first"
  exit 1
fi

fail=0
for s in $SUITES; do
  [ -f "demo/$s.js" ] || { printf "  %-12s no such suite\n" "$s"; fail=1; continue; }
  out=$(node "demo/$s.js" 2>&1)
  line=$(echo "$out" | grep -E "^[0-9]+ passed" | head -1)
  if [ -z "$line" ]; then
    printf "  %-12s DID NOT REPORT — %s\n" "$s" \
      "$(echo "$out" | grep -vE '^\s*$' | tail -1 | cut -c1-90)"
    fail=1
  else
    printf "  %-12s %s\n" "$s" "$line"
    echo "$line" | grep -q "^[0-9]* passed, 0 failed" || fail=1
  fi
done
rm -f tests/drafts/workbench-probe.json
exit $fail
