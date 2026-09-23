# demo — the product, captured and narrated

Two steps, both reproducible from a clean checkout.

## 1. Capture

`capture.js` drives the console with Playwright and writes one PNG per beat of
the story. Frames are **element** screenshots, not viewports, so each slide is a
single panel rather than whatever happened to be on screen.

```bash
python console.py &                      # console on :4100
# start the mock from the console, or:
python mockd.py --spec "$(python ../project.py show | head -1 | awk '{print $3}')" --port 4010 &

cd demo && npm install playwright && npx playwright install chromium
node capture.js                          # -> shots/*.png + shots.json
(cd shots && zip -q ../mockd-demo.zip *.png)
```

Nothing is staged: the numbers on those slides are whatever your spec actually
says at capture time. Re-run it after the spec changes and the deck re-states
the truth.

## 2. Narrate

`walkthrough.yaml` is a narration script for the captured slides: one line per
panel, four chapters. It deliberately says out loud how many of the payloads
are inferred rather than documented — a demo that hides that is the thing this
whole project exists to argue against.

Rendering it to video needs a slideshow narrator that takes a `.zip` of images
plus a script. Because the document is images rather than a PDF there is no
text to address by name, so every `focus` is a page (`p1` … `p17`) rather than
a heading.

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
