#!/bin/bash
# Smoke test for mockd against apis.json — starts the server, walks every
# capability a UI dev or QA engineer needs, kills it. Each block is a copy-
# pasteable curl, which is the point: this doubles as the usage guide.
cd "$(dirname "$0")"
PORT=${PORT:-4010}
PY=${PY:-python3}
BASE="localhost:$PORT"

rm -f logs/requests.jsonl server.log
$PY mockd.py --spec apis.json --overlay mock_overlay.json --port "$PORT" --stateful \
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
curl -s "$BASE/api/v1/widgets" | j | head -20

hr "3. An endpoint the spec declares as schema:{} still answers usefully"
curl -s "$BASE/api/v1/auth/me" | j

hr "4. Bad request -> 422 in the shape the real API returns (loc / msg / type)"
curl -s -X POST "$BASE/api/v1/widgets" \
     -H "Content-Type: application/json" -d '{"title":"","description":123}' | j

hr "5. Bad and missing query params -> 422, same shape"
curl -s "$BASE/api/v1/account/list?status=NotAStatus" | j
curl -s "$BASE/api/v1/account/list?page=abc" | j
echo "  and a missing REQUIRED query param:"
curl -s "$BASE/api/v1/reference/cities" | j

hr "6. Error paths: force any documented status (QA's main lever)"
OP="$BASE/api/v1/widgets/3fa85f64-5717-4562-b3fc-2c963f66afa6"
for S in 401 403 404 409 422 500; do
  printf '  %s -> ' "$S"
  curl -s -o /tmp/mockd_err.$$ -w "%{http_code}  " "$OP" -H "X-Mock-Status: $S"
  head -c 110 /tmp/mockd_err.$$; echo
done
rm -f /tmp/mockd_err.$$

hr "7. 65 of 96 operations document only 200+422 — forcing a 500 there needs a flag"
echo "  GET /api/v1/group/read/{group_id} documents only 200 and 422."
echo "  This server runs with --allow-undocumented-status, so QA can still force one:"
curl -s -w "\n  -> %{http_code}\n" "$BASE/api/v1/group/read/abc" -H "X-Mock-Status: 500"
echo "  Without that flag the mock refuses and prints the documented list instead,"
echo "  which is how you find out the spec is missing its error responses."

hr "8. Named scenarios from the overlay (empty list / full page)"
printf '  empty     -> '
curl -s "$BASE/api/v1/account/list" -H "X-Mock-Scenario: empty" \
  | $PY -c "import sys,json; d=json.load(sys.stdin); print(d['message'], '| items:', len(d['data']['items']))"
printf '  page_full -> '
curl -s "$BASE/api/v1/account/list" -H "X-Mock-Scenario: page_full" \
  | $PY -c "import sys,json; d=json.load(sys.stdin); print('items:', len(d['data']['items']), '| total:', d['data']['total'])"

hr "9. Null-handling: every nullable field comes back null"
curl -s "$BASE/api/v1/widget-links" -H "X-Mock-Nulls: on" | j | head -12

hr "10. Slow response, for loader and timeout tests"
curl -s -o /dev/null -w "  X-Mock-Delay: 1200 took %{time_total}s\n" \
     "$BASE/api/v1/reference/countries" -H "X-Mock-Delay: 1200"

hr "11. Stateful CRUD across this API's verb-in-path routes"
CREATED=$(curl -s -X POST "$BASE/api/v1/account/create" -H "Content-Type: application/json" -d '{
  "username":"qa.user","email":"qa.user@example.com","first_name":"QA","last_name":"Bot",
  "phone_number":"+919812345678","country_code":"IN",
  "role_ids":["3fa85f64-5717-4562-b3fc-2c963f66afa6"]}')
UID_=$(echo "$CREATED" | $PY -c "
import sys, json
b = json.load(sys.stdin)
print(((b.get('data') if isinstance(b.get('data'), dict) else None) or b).get('id', ''))" 2>/dev/null)
echo "  created id: $UID_"
printf '  read   -> %s\n' "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/v1/account/read/$UID_")"
printf '  update -> %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$BASE/api/v1/account/update/$UID_" \
   -H 'Content-Type: application/json' -d '{"username":"qa.user","email":"qa.user@example.com","first_name":"Updated","last_name":"Bot","phone_number":"+919812345678","country_code":"IN","role_ids":["3fa85f64-5717-4562-b3fc-2c963f66afa6"]}')"
printf '  delete -> %s\n' "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$BASE/api/v1/account/delete/$UID_")"
printf '  read after delete -> %s (a real backend would 404 too)\n' \
   "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/v1/account/read/$UID_")"

hr "12. Unknown path / wrong method"
curl -s "$BASE/api/v1/nope" | j
curl -s -X POST "$BASE/api/v1/account/list" | j

hr "13. Live reload — add an endpoint to the spec while the server runs"
cp apis.json /tmp/apis_backup.$$
$PY - <<'PYEOF'
import json
d = json.load(open("apis.json"))
d["paths"]["/api/v1/smoketest/list"] = {"get": {
    "tags": ["SmokeTest"], "summary": "List Smoke Tests", "operationId": "smoke_list",
    "responses": {"200": {"description": "Successful Response",
                          "content": {"application/json": {"schema": {}}}}}}}
json.dump(d, open("apis.json", "w"))
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
mv /tmp/apis_backup.$$ apis.json
sleep 0.5

hr "14. Request log — the evidence trail"
curl -s "$BASE/_mock/log" | $PY -c "
import sys, json
for e in json.load(sys.stdin)[-6:]:
    v = f\"violations={len(e['validation_errors'])}\" if e['validation_errors'] else ''
    print(f\"  {e['ts']} {e['method']:6s} {e['path'][:46]:46s} -> {e['status']}  {e.get('source') or ''} {v}\")
"

echo; echo "smoke test done."
