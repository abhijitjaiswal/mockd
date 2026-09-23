# demo — the product, captured and narrated

Two steps, both reproducible from a clean checkout.

## 1. Capture

`capture.js` drives the console with Playwright and writes one PNG per beat of
the story. Frames are **element** screenshots, not viewports, so each slide is a
single panel rather than whatever happened to be on screen.

```bash
python console.py &                      # console on :4100
# start the mock from the console, or:
python mockd.py --spec apis.json --overlay mock_overlay.json --port 4010 &

cd demo && npm install playwright && npx playwright install chromium
node capture.js                          # -> shots/*.png + shots.json
(cd shots && zip -q ../mockd-demo.zip *.png)
```

Nothing is staged: the numbers on those slides are whatever `apis.json` actually
says at capture time. Re-run it after the spec changes and the deck re-states
the truth.

## 2. Narrate

`walkthrough.yaml` is a script for [troupe](http://127.0.0.1:8788/walkthrough) —
it takes a PDF or a `.zip` of slide images plus a script, and renders a film
where the camera moves on the spoken word. Upload `mockd-demo.zip`, paste
everything below `defaults:`, and render.

Because the document is images rather than a PDF, there is no text to address by
name: every `focus` is a page (`p1` … `p17`), not `h1:Something`. Keep that in
mind when editing — a heading target will not resolve.

The script is 4 chapters / 27 lines, one per panel, and deliberately says out
loud that 65 of the 96 payloads are inferred. A demo that hides that is the
thing this whole project exists to argue against.

## The render

`mockd-walkthrough.mp4` — 4 minutes 7 seconds, 4 chapters, 27 narrated lines,
rendered draft quality (854×480). Re-render at full quality by unticking
`draft` in the bench.

```bash
python - <<'EOF'   # or just use the bench UI
import base64, json, urllib.request, yaml
data = base64.b64encode(open("mockd-demo.zip","rb").read()).decode()
up = json.loads(urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:8788/api/walkthrough/upload",
    data=json.dumps({"project": "", "name": "mockd-demo.zip", "data": data}).encode(),
    headers={"Content-Type": "application/json"})).read())

script = yaml.safe_load(open("walkthrough.yaml"))
script["source"] = {"kind": up["kind"], "path": up["path"]}
job = json.loads(urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:8788/api/walkthrough/render",
    data=json.dumps({"project": up["project"], "script": script, "draft": True}).encode(),
    headers={"Content-Type": "application/json"})).read())
print(job)   # then poll /api/job/<job>
EOF
```

The upload is JSON with a base64 `data` field, not multipart — worth knowing if
you script it.

## Checking the console still works

`e2e.js` clicks through every section the way a person would and asserts the
parts are wired to each other — 52 checks, and it fails on any uncaught JS error.
`assert.js` covers the assertion editor, `runnow.js` and `chain.js` the in-dialog
run, and `progress.js` the live per-operation progress.

```bash
node e2e.js        # 52 passed, 0 failed, no JS errors
node assert.js     # 10 passed, 0 failed
node runnow.js     #  8 passed, 0 failed
node chain.js      #  4 passed, 0 failed
node progress.js   #  9 passed, 0 failed
```

Both need the console on :4100 with the mock started. They caught real bugs:
"Fill sample body" reading the last-clicked row instead of the typed path, and
`/api/tests/one` resolving the wrong file when a section name exists as both a
draft and a shared suite.

## Files

```
capture.js              the Playwright run
e2e.js                  clicks through every section and checks the wiring
assert.js               covers the assertion editor
walkthrough.yaml        the narration script
shots/                  17 captured slides + shots.json (page -> what it shows)
mockd-demo.zip          those slides, ready to upload
mockd-walkthrough.mp4   the rendered film
```
