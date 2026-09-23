#!/bin/bash
# Smoke test for mockd against the project spec — starts the server, walks every
# capability a UI dev or QA engineer needs, kills it. Each block is a copy-
# pasteable curl, which is the point: this doubles as the usage guide.
cd "$(dirname "$0")"
PORT=${PORT:-4010}
PY=${PY:-python3}
BASE="localhost:$PORT"

# Which endpoints to walk is a fact about the spec being served, not about this
# script. Ask the document rather than hardcoding one project's paths — that is
# what made an earlier version of this file call routes that existed nowhere.
eval "$($PY - <<'PYEOF'
import project
from mockd import Source, Spec

spec_path = project.active_spec()
text, _ = Source(spec_path, poll=0, cache_dir="logs").read(force=True)
spec = Spec(text=text, origin=spec_path)

def pick(method, params, need_body=False):
    for r in spec.routes:
        if r["method"] != method or len(r["path_params"]) != params:
            continue
        if need_body and not (r["request_body"] or {}).get("content"):
            continue
        return r["path"]
    return ""

collection = pick("GET", 0)
item       = pick("GET", 1)
create     = pick("POST", 0, need_body=True) or pick("POST", 0)
update     = pick("PUT", 1) or pick("PATCH", 1)
delete     = pick("DELETE", 1)
second     = next((r["path"] for r in spec.routes
                   if r["method"] == "GET" and not r["path_params"]
                   and r["path"] != collection), collection)

def prefix(path):
    """'/api/v1/account/read/{account_id}' -> '/api/v1/account/read/' so an id can be
    appended; empty when the parameter is not the last segment."""
    return path.split("{", 1)[0] if path and path.rstrip("/").endswith("}") else ""

import random
import postman as pm
rng = random.Random("smoke")

def concrete(path):
    out = path
    for r in spec.routes:
        if r["path"] != path:
            continue
        for prm in r["parameters"]:
            if prm.get("in") == "path":
                out = out.replace("{%s}" % prm["name"],
                                  pm.sample_value(prm.get("schema") or {}, rng))
    return out

for name, value in (("C_LIST", collection), ("C_ITEM", concrete(item)),
                    ("C_CREATE", create), ("C_OTHER", second),
                    ("C_READ_P", prefix(item)), ("C_UPDATE_P", prefix(update)),
                    ("C_DELETE_P", prefix(delete)),
                    ("C_UPDATE_M", "PUT" if pick("PUT", 1) else "PATCH")):
    print(f'{name}="{value}"')
PYEOF
)"
: "${C_LIST:?could not find a listable GET in the spec}"
# An empty one would silently become "GET /", which reports 404 and looks like
# a broken mock rather than a missing value.
: "${C_OTHER:=$C_LIST}"
: "${C_CREATE:=$C_LIST}"
: "${C_ITEM:=$C_LIST}"
for _v in C_READ_P C_UPDATE_P C_DELETE_P; do
  if [ -z "${!_v}" ]; then
    echo "  note: the spec has no single-parameter route for ${_v%_P}; skipping that step"
  fi
done
echo "walking: $C_LIST  $C_ITEM  $C_CREATE"

rm -f logs/requests.jsonl server.log
SPEC=${SPEC:-$($PY -c "import project; print(project.active_spec())")}
export SPEC
OVERLAY=""
[ -f mock_overlay.json ] && OVERLAY="--overlay mock_overlay.json"
$PY mockd.py --spec "$SPEC" $OVERLAY --port "$PORT" --stateful \
    --allow-undocumented-status > server.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT

for _ in $(seq 1 40); do
  curl -sf "$BASE/_mock/routes" >/dev/null 2>&1 && break
  sleep 0.25
done
if ! curl -sf "$BASE/_mock/routes" >/dev/null 2>&1; then
  echo "server failed to start:"; cat server.log; exit 1
fi

j() { $PY -m json.tool 2>/dev/null || cat; }
hr() { printf '\n=== %s ===\n' "$1"; }

hr "1. Every operation in the spec is live"
curl -s "$BASE/_mock/routes" | $PY -c "
import sys, json, collections
rs = json.load(sys.stdin)
print(f'  {len(rs)} operations loaded')
for src, n in sorted(collections.Counter(r['body_source'] for r in rs).items()):
    print(f'    {src:14s} {n}')
"

hr "2. A real payload, not {\"message\": \"Successful Response\"}"
curl -s "$BASE$C_LIST" | j | head -20

hr "3. An endpoint the spec declares as schema:{} still answers usefully"
curl -s "$BASE$C_OTHER" | j

hr "4. Bad request -> 422 in the shape the real API returns (loc / msg / type)"
curl -s -X POST "$BASE$C_CREATE" \
     -H "Content-Type: application/json" -d '{"title":"","description":123}' | j

hr "5. Bad and missing query params -> 422, same shape"
curl -s "$BASE$C_LIST?status=NotAStatus" | j
curl -s "$BASE$C_LIST?page=abc" | j
echo "  and a missing REQUIRED query param:"
curl -s "$BASE$C_OTHER" | j

hr "6. Error paths: force any documented status (QA's main lever)"
OP="$BASE$C_ITEM"
for S in 401 403 404 409 422 500; do
  printf '  %s -> ' "$S"
  curl -s -o /tmp/mockd_err.$$ -w "%{http_code}  " "$OP" -H "X-Mock-Status: $S"
  head -c 110 /tmp/mockd_err.$$; echo
done
rm -f /tmp/mockd_err.$$

hr "7. 65 of 96 operations document only 200+422 — forcing a 500 there needs a flag"
echo "  the item read documents only 200 and 422."
echo "  This server runs with --allow-undocumented-status, so QA can still force one:"
curl -s -w "\n  -> %{http_code}\n" "$BASE${C_READ_P}not-a-real-id" -H "X-Mock-Status: 500"
echo "  Without that flag the mock refuses and prints the documented list instead,"
echo "  which is how you find out the spec is missing its error responses."

hr "8. Named scenarios from the overlay (empty list / full page)"
printf '  empty     -> '
curl -s "$BASE$C_LIST" -H "X-Mock-Scenario: empty" \
  | $PY -c "import sys,json; d=json.load(sys.stdin); print(d['message'], '| items:', len(d['data']['items']))"
printf '  page_full -> '
curl -s "$BASE$C_LIST" -H "X-Mock-Scenario: page_full" \
  | $PY -c "import sys,json; d=json.load(sys.stdin); print('items:', len(d['data']['items']), '| total:', d['data']['total'])"

hr "9. Null-handling: every nullable field comes back null"
curl -s "$BASE$C_OTHER" -H "X-Mock-Nulls: on" | j | head -12

hr "10. Slow response, for loader and timeout tests"
curl -s -o /dev/null -w "  X-Mock-Delay: 1200 took %{time_total}s\n" \
     "$BASE$C_OTHER" -H "X-Mock-Delay: 1200"

hr "11. Stateful CRUD across this API's verb-in-path routes"
CREATED=$(curl -s -X POST "$BASE$C_CREATE" -H "Content-Type: application/json" -d '{
  "username":"qa.user","email":"qa.user@example.com","first_name":"QA","last_name":"Bot",
  "phone_number":"+919812345678","country_code":"IN",
  "role_ids":["3fa85f64-5717-4562-b3fc-2c963f66afa6"]}')
UID_=$(echo "$CREATED" | $PY -c "
import sys, json
b = json.load(sys.stdin)
print(((b.get('data') if isinstance(b.get('data'), dict) else None) or b).get('id', ''))" 2>/dev/null)
echo "  created id: $UID_"
if [ -n "$UID_" ] && [ -n "$C_READ_P" ]; then
printf '  read   -> %s\n' "$(curl -s -o /dev/null -w '%{http_code}' "$BASE$C_READ_P$UID_")"
printf '  update -> %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -X "$C_UPDATE_M" "$BASE$C_UPDATE_P$UID_" \
   -H 'Content-Type: application/json' -d '{"username":"qa.user","email":"qa.user@example.com","first_name":"Updated","last_name":"Bot","phone_number":"+919812345678","country_code":"IN","role_ids":["3fa85f64-5717-4562-b3fc-2c963f66afa6"]}')"
printf '  delete -> %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$BASE$C_DELETE_P$UID_")"
printf '  read after delete -> %s (a real backend would 404 too)\n' \
   "$(curl -s -o /dev/null -w '%{http_code}' "$BASE$C_READ_P$UID_")"
else
  echo "  no id came back from create, or the spec has no item route — lifecycle skipped"
fi

hr "12. Unknown path / wrong method"
curl -s "$BASE/api/v1/nope" | j
curl -s -X POST "$BASE$C_LIST" | j

hr "13. Live reload — add an endpoint to the spec while the server runs"
cp "$SPEC" "/tmp/spec_backup.$$"
$PY - <<'PYEOF'
import json
import os
d = json.load(open(os.environ["SPEC"]))
d["paths"]["/api/v1/smoketest/list"] = {"get": {
    "tags": ["SmokeTest"], "summary": "List Smoke Tests", "operationId": "smoke_list",
    "responses": {"200": {"description": "Successful Response",
                          "content": {"application/json": {"schema": {}}}}}}}
json.dump(d, open(os.environ["SPEC"], "w"))
PYEOF
sleep 0.5
printf '  GET /api/v1/smoketest/list -> %s  (no restart, no overlay entry)\n' \
   "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/v1/smoketest/list")"
curl -s "$BASE/_mock/drift" | $PY -c "
import sys, json
d = json.load(sys.stdin)
print('  spec generation:', d['generation'], '| added on last reload:', d['last_change']['added'])
print('  operations with no curated overlay entry:', len(d['uncurated']))
"
mv "/tmp/spec_backup.$$" "$SPEC"
sleep 0.5

hr "14. Request log — the evidence trail"
curl -s "$BASE/_mock/log" | $PY -c "
import sys, json
for e in json.load(sys.stdin)[-6:]:
    v = f\"violations={len(e['validation_errors'])}\" if e['validation_errors'] else ''
    print(f\"  {e['ts']} {e['method']:6s} {e['path'][:46]:46s} -> {e['status']}  {e.get('source') or ''} {v}\")
"

hr "15. A pasted credential is normalised before it is sent"
# A trailing newline or a pair of quotes around a token changes the value the
# server compares, and the server then says "invalid token" — which reads as a
# bad credential rather than a bad paste.
$PY -c "
import environments as e
TOKEN = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.SIG'
WANT = 'access_token=' + TOKEN
forms = {
    'plain':            WANT,
    'quoted':           '\"' + WANT + '\"',
    'trailing newline': WANT + '\n',
    'leading space':    ' ' + WANT,
    'copied with attrs': WANT + '; Path=/; HttpOnly',
    'space after =':    'access_token= ' + TOKEN,
}
bad = []
for label, raw in forms.items():
    env = {'base_url': 'https://x', 'auth': {'mode': 'none'}, 'headers': {'Cookie': raw}}
    got = e.authenticate(env)[0].get('Cookie')
    print(f'  {label:20s} -> {\"ok\" if got == WANT else \"WRONG: \" + repr(got)}')
    if got != WANT:
        bad.append(label)
raise SystemExit(1 if bad else 0)
" || echo "  ^^ a paste form was sent unnormalised"

echo; echo "smoke test done."
