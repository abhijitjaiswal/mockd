/**
 * impact.js — what mockd notices by itself.
 *
 * Two things nobody asks it to check. A document that has just been loaded is
 * compared with the one in use before it is put to use, and the comparison is
 * one sentence. And Home lists what has been noticed — here, a test that
 * cannot pass until somebody gives it a value — each with the one thing to do.
 *
 * Nothing is switched: the document is loaded and cancelled. The probe test
 * lives in its own draft suite and is removed afterwards.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const os = require("os");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + String(d).slice(0, 300)}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const STAMP = Date.now().toString(36);
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "mockd-impact-"));
const OTHER = path.join(TMP, `probe-other-${STAMP}.json`);
const SUITE = path.join(DIR, "tests", "drafts", "noticed-probe.json");
process.on("exit", () => {
  for (const f of [SUITE, path.join(DIR, "specs", path.basename(OTHER))]) {
    try { fs.rmSync(f, { force: true }); } catch { /* gone */ }
  }
  try { fs.rmSync(TMP, { recursive: true, force: true }); } catch { /* gone */ }
});

(async () => {
  fs.writeFileSync(OTHER, JSON.stringify({ openapi: "3.1.0",
    info: { title: "Some Other API", version: "0.1" },
    paths: { "/elsewhere": { get: { responses: { 200: { description: "ok" } } } } } }));
  fs.mkdirSync(path.dirname(SUITE), { recursive: true });
  fs.writeFileSync(SUITE, JSON.stringify({ name: "noticed-probe", data: {}, cases: [], scenarios: [{
    id: "noticed-probe-flow", name: "a probe mockd should notice", kind: "e2e",
    levels: ["regression"], priority: "P3", data: { probeKey: "<a real key only you know>" },
    steps: [{ role: "target", name: "ask for the routes",
              request: { method: "GET", path: "/_mock/routes", query: { key: "{{probeKey}}" } },
              assertions: [{ type: "status", equals: 200 }] }] }] }, null, 2));

  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  const project = async () => p.evaluate(async () => (await fetch("/api/project")).json());
  const original = (await project()).spec;

  // ------------------------------------------------ on Home, without asking
  await p.waitForFunction(() => document.querySelectorAll("#homeNoticed .noticed").length > 0,
                          null, { timeout: 20000 }).catch(() => {});
  check("Home has a list of what mockd noticed", await p.locator("#homeNoticedCard").isVisible());
  const need = p.locator('.noticed[data-noticed="needs-values"]');
  check("a test waiting for a value is on it", await need.count() === 1,
        await p.locator("#homeNoticed").textContent());
  check("said in words", /waiting for a value only you know/.test(await need.textContent()),
        await need.textContent());
  check("each item has one thing to do about it", await need.locator("[data-noticedgo]").count() === 1);
  const items = await p.locator("#homeNoticed .noticed").count();
  const buttons = await p.locator("#homeNoticed button").count();
  check("and no more than two buttons each", buttons <= items * 2, `${buttons} for ${items}`);

  await need.locator("[data-noticedgo]").click();
  await p.waitForTimeout(1500);
  check("its button opens Tests", await p.locator('.view[data-view="tests"]').isVisible());
  check("showing only those tests, and saying so",
        await p.locator("#libOnly").isVisible()
        && /waiting for a value/.test(await p.locator("#libOnly").textContent()),
        await p.locator("#libOnly").textContent());
  const rows = await p.locator("#libList .librow").allTextContents();
  check("the probe is among them", rows.some((r) => /a probe mockd should notice/.test(r)), rows.join(" | ").slice(0, 200));
  check("marked as needing a value",
        rows.some((r) => /a probe mockd should notice/.test(r) && /needs a value/.test(r)),
        rows.map((r) => r.replace(/\s+/g, " ")).join(" | ").slice(0, 300));
  const narrowed = rows.length;
  await p.locator("#libOnlyOff").click();
  await p.waitForTimeout(500);
  check("Show all tests brings the rest back", await p.locator("#libOnly").isHidden()
        && await p.locator("#libList .librow").count() >= narrowed);

  // --------------------------------- a document loaded, compared unasked
  await p.locator('nav.side a[data-view="source"]').click();
  await p.waitForTimeout(1200);
  await p.locator("#docFile").setInputFiles(OTHER);
  await p.locator("#docLoad").click();
  await p.waitForFunction(() => /Compared with the one in use/.test(
    (document.querySelector("#docImpact") || {}).textContent || ""), null, { timeout: 30000 }).catch(() => {});
  const said = ((await p.locator("#docImpact").textContent()) || "").replace(/\s+/g, " ");
  check("loading a document says what it would change, before it is used",
        /Compared with the one in use:/.test(said), said);
  check("a document that drops what exists is called breaking", /would break something that works today/.test(said), said);
  check("in one sentence, with the detail folded away",
        await p.locator("#docImpact details").count() === 1
        && !(await p.locator("#docImpact details").evaluate((el) => el.open)));
  await p.locator("#docImpact summary").click();
  await p.waitForTimeout(300);
  check("the detail names what was removed",
        /operation removed/.test(await p.locator("#docImpact details").textContent()),
        (await p.locator("#docImpact details").textContent()).slice(0, 200));
  check("nothing has been switched", (await project()).spec === original);
  await p.locator("#docCancel").click();
  await p.waitForTimeout(300);
  check("and Cancel leaves it that way", (await project()).spec === original
        && await p.locator("#docFound").isHidden());

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
