/**
 * watch.js — learning from real traffic, from the Servers screen.
 *
 * The suite stands up a small "real API" of its own, adds it as a server,
 * and then does what a person would: starts watching, sends traffic through
 * the address it is given, and reads what mockd worked out — including an
 * endpoint the document does not have. Then the other way in: a recording
 * exported from a browser.
 *
 * Its server is local, so nothing outside this machine is touched. The shared
 * server file, the values file, any recording that was already there and the
 * draft tests folder are all put back as they were.
 */
const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + String(d).slice(0, 300)}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const KEEP = ["environments.json", ".env", "logs/recorded.jsonl", "logs/recorded.meta.json"].map((name) => {
  const file = path.join(DIR, name);
  return { file, before: fs.existsSync(file) ? fs.readFileSync(file) : null };
});
const DRAFTS = path.join(DIR, "tests", "drafts");
const draftsBefore = new Set(fs.existsSync(DRAFTS) ? fs.readdirSync(DRAFTS) : []);
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "mockd-watch-"));
process.on("exit", () => {
  for (const { file, before } of KEEP) {
    try { if (before === null) fs.rmSync(file, { force: true }); else fs.writeFileSync(file, before); }
    catch { /* nothing to restore */ }
  }
  try {
    for (const name of fs.readdirSync(DRAFTS)) {
      if (!draftsBefore.has(name) && /^recorded/.test(name)) fs.rmSync(path.join(DRAFTS, name), { force: true });
    }
  } catch { /* no drafts folder */ }
  try { fs.rmSync(TMP, { recursive: true, force: true }); } catch { /* gone */ }
});

(async () => {
  // a "real API": answers anything with JSON, and remembers what it was sent
  const seen = [];
  const real = http.createServer((req, res) => {
    seen.push({ method: req.method, url: req.url, auth: req.headers.authorization || "" });
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true, path: req.url }));
  });
  await new Promise((r) => real.listen(0, "127.0.0.1", r));
  const REAL = `http://127.0.0.1:${real.address().port}`;
  const NAME = "watch-" + Date.now().toString(36);

  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  const toast = async () => ((await p.locator("#toast").textContent().catch(() => "")) || "").trim();
  const flat = (s) => (s || "").replace(/\s+/g, " ").trim();
  const post = (url, body) => p.evaluate(async ([u, x]) => (await fetch(u, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(x || {}) })).json(), [url, body]);

  let watching = false;
  try {
    await post("/api/record/stop"); await post("/api/record/clear");
    const prefix = NAME.toUpperCase().replace(/[^A-Z0-9]+/g, "_");
    await post("/api/environments/create", { name: NAME, mode: "token" });
    await post("/api/environments/vars", { values: { [`${prefix}_BASE_URL`]: REAL, [`${prefix}_TOKEN`]: "watch-secret-token" } });

    await p.locator('nav.side a[data-view="environments"]').click();
    await p.waitForFunction(() => !!document.querySelector("#watchStart, #watchLoadHar"), null, { timeout: 15000 }).catch(() => {});

    // -------------------------------------------------- what is on offer
    check("Servers has one card for learning from real traffic", await p.locator("#watchCard").isVisible());
    check("it says what it is for in a sentence",
          /where the API and the document disagree/.test(await p.locator("#watchIdle").textContent()));
    const offered = await p.evaluate(() =>
      [...document.querySelectorAll("#watchCard button, #watchCard select")].filter((el) => el.offsetParent !== null).length);
    check("with three controls: which server, start, or load a recording", offered === 3, String(offered));
    check("the mock is not offered as a server to watch",
          !(await p.locator("#watchServer option").allTextContents()).some((o) => /^mock/.test(o)));

    // --------------------------------------------------------- watching
    await p.locator("#watchServer").selectOption(NAME);
    await p.locator("#watchStart").click();
    await p.waitForFunction(() => !!document.querySelector("#watchStop"), null, { timeout: 30000 }).catch(() => {});
    watching = await p.locator("#watchStop").count() === 1;
    const on = flat(await p.locator("#watchIdle").textContent());
    const address = (on.match(/http:\/\/localhost:\d+/) || [""])[0];
    check("Start watching says where to point the app", watching && !!address, on || await toast());

    const first = await p.evaluate(async (a) => {
      const r = await fetch(a + "/_probe/thing?x=1");
      return { status: r.status, body: await r.json() };
    }, address).catch((e) => ({ error: String(e) }));
    check("a call to that address is answered by the real server",
          first.status === 200 && first.body && first.body.path === "/_probe/thing?x=1", JSON.stringify(first));
    await p.evaluate(async (a) => { await fetch(a + "/_probe/thing"); await fetch(a + "/_probe/thing"); }, address);
    check("the real server got it signed in, without the app holding the token",
          seen.length >= 3 && seen.every((s) => s.auth === "Bearer watch-secret-token"), JSON.stringify(seen.slice(0, 2)));
    await p.waitForFunction(() => /3 calls so far/.test((document.querySelector("#watchCount") || {}).textContent || ""),
                            null, { timeout: 15000 }).catch(() => {});
    check("the count on screen follows along", /3 calls so far/.test(await p.locator("#watchCount").textContent()),
          await p.locator("#watchCount").textContent());

    await p.locator("#watchStop").click();
    await p.waitForFunction(() => !!document.querySelector("#watchStart"), null, { timeout: 15000 }).catch(() => {});
    watching = false;
    check("Stop ends it", await p.locator("#watchStart").count() === 1);

    // ----------------------------------------------------- what it found
    const found = flat(await p.locator("#watchFound").textContent());
    check("what was seen is summed up in a sentence",
          /Across 3 calls: 1 endpoint the document does not have/.test(found), found);
    check("and the endpoint is named", /not in the document\s*GET \/_probe\/thing/.test(found), found);
    const recording = fs.readFileSync(path.join(DIR, "logs", "recorded.jsonl"), "utf8");
    check("the recording on disk holds no token", !recording.includes("watch-secret-token") && !/Bearer/.test(recording));

    await p.locator("#watchTest").click();
    await p.waitForTimeout(800);
    check("making a test from calls the document does not have is declined, with the reason",
          /nothing to make a test from/i.test(await toast()), await toast());

    await p.locator('nav.side a[data-view="home"]').click();
    await p.waitForFunction(() => !!document.querySelector('.noticed[data-noticed="real-api-differs"]'),
                            null, { timeout: 15000 }).catch(() => {});
    const item = p.locator('.noticed[data-noticed="real-api-differs"]');
    check("Home lists it among what mockd noticed", await item.count() === 1
          && /and the document disagree in 1 place/.test(await item.textContent()),
          await p.locator("#homeNoticedCard").textContent());
    await item.locator("[data-noticedgo]").click();
    await p.waitForTimeout(800);
    check("and its button goes to where it is shown", await p.locator("#watchFound").isVisible());

    await p.locator("#watchClear").click();
    await p.waitForFunction(() => !document.querySelector("#watchClear"), null, { timeout: 10000 }).catch(() => {});
    check("Clear puts the recording away", (await p.locator("#watchFound").textContent()).trim() === "");

    // ------------------------------------ the other way in: from a browser
    const routes = await p.evaluate(async () => {
      try { return await (await fetch("/api/routes")).json(); } catch { return null; }
    });
    const mockRoutes = await p.evaluate(async () => {
      const state = await (await fetch("/api/state")).json();
      try { return await (await fetch(state.base_url.replace("127.0.0.1", "localhost") + "/_mock/routes")).json(); }
      catch { return null; }
    });
    const list = (mockRoutes && (mockRoutes.routes || mockRoutes)) || (routes && routes.routes) || [];
    const plain = (list || []).find((r) => (r.method || "").toUpperCase() === "GET" && r.path && !r.path.includes("{"));
    if (!plain) {
      check("a documented endpoint could be found to build a recording from", false, JSON.stringify(list).slice(0, 200));
    } else {
      const har = path.join(TMP, "session.har");
      const entry = (url, body) => ({ startedDateTime: new Date().toISOString(), time: 12,
        request: { method: "GET", url }, response: { status: 200,
          content: { mimeType: "application/json", text: JSON.stringify(body) } } });
      fs.writeFileSync(har, JSON.stringify({ log: { entries: [
        { request: { method: "GET", url: "https://static.example.test/app.js" },
          response: { status: 200, content: { mimeType: "text/javascript", text: "x" } } },
        entry("https://api.example.test" + plain.path, {}),
        entry("https://api.example.test/_probe/from-browser", { n: 1 })] } }));
      const junk = path.join(TMP, "notes.har");
      fs.writeFileSync(junk, "these are my notes");

      await p.locator("#watchHar").setInputFiles(junk);
      await p.waitForFunction(() => /not a browser recording/i.test(document.querySelector("#toast").textContent),
                              null, { timeout: 10000 }).catch(() => {});
      check("a file that is not a recording is refused, with how to make one",
            /Save all as HAR/.test(await toast()), await toast());

      await p.locator("#watchHar").setInputFiles(har);
      await p.waitForFunction(() => /Across 2 calls/.test(document.querySelector("#watchFound").textContent),
                              null, { timeout: 20000 }).catch(() => {});
      const fromBrowser = flat(await p.locator("#watchFound").textContent());
      check("a browser recording is read, keeping only the API's calls",
            /api\.example\.test/.test(fromBrowser) && /Across 2 calls/.test(fromBrowser), fromBrowser || await toast());
      check("the same things are worked out from it", /GET \/_probe\/from-browser/.test(fromBrowser), fromBrowser);

      await p.locator("#watchTest").click();
      await p.waitForFunction(() => document.querySelector('.view[data-view="tests"]').classList.contains("on"),
                              null, { timeout: 20000 }).catch(() => {});
      await p.waitForTimeout(1200);
      check("Make a test from this opens the new test in Tests",
            await p.locator("#libOnly").isVisible()
            && /made from the recording/.test(await p.locator("#libOnly").textContent()), await toast());
      check("as a single row, already opened",
            await p.locator("#libList .librow").count() === 1
            && /What was recorded/.test(await p.locator("#libList .librow").first().textContent()),
            flat(await p.locator("#libList").textContent()).slice(0, 200));
      await p.locator('nav.side a[data-view="environments"]').click();
      await p.waitForTimeout(800);
      if (await p.locator("#watchClear").count()) await p.locator("#watchClear").click();
    }
  } finally {
    if (watching) await post("/api/record/stop").catch(() => {});
    await post("/api/record/clear").catch(() => {});
    real.close();
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
