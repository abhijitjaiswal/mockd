/**
 * needs.js — a value only a person knows.
 *
 * When nothing in the API can supply a value, a test carries a placeholder
 * rather than an invented one. That is only honest if the screen then says so
 * and lets you put the real value in where you are looking. The suite makes
 * such a test, finds it flagged in the list, fills it by clicking, and checks
 * the file.
 *
 * Works in its own draft suite and removes it afterwards.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + String(d).slice(0, 300)}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const FILE = path.join(DIR, "tests", "drafts", "needs-probe.json");
process.on("exit", () => { try { fs.rmSync(FILE, { force: true }); } catch { /* gone */ } });

(async () => {
  fs.mkdirSync(path.dirname(FILE), { recursive: true });
  fs.writeFileSync(FILE, JSON.stringify({ name: "needs-probe", data: {}, cases: [], scenarios: [{
    id: "needs-probe-flow", name: "a probe that waits for a value", kind: "e2e",
    levels: ["regression"], priority: "P2",
    data: { probeKey: "<a real key only you know>" },
    steps: [{ role: "target", name: "ask for the routes",
              request: { method: "GET", path: "/_mock/routes", query: { key: "{{probeKey}}" } },
              assertions: [{ type: "status", equals: 200 }] }] }] }, null, 2));

  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  const toast = async () => ((await p.locator("#toast").textContent().catch(() => "")) || "").trim();

  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForTimeout(1500);
  await p.locator("#libSearch").fill("waits for a value");
  await p.waitForTimeout(600);
  const row = p.locator("#libList .librow").first();
  check("the test is in the list", /a probe that waits for a value/.test(await row.textContent()));
  check("and is marked as needing a value, not as failing or never run",
        /needs a value/.test(await row.locator(".top .libres").textContent()),
        await row.locator(".top .libres").textContent());

  await row.locator(".top .nm").click();
  await p.waitForTimeout(400);
  const need = row.locator('[data-libneed="probeKey"]');
  check("opening it says what is missing, in words", await need.count() === 1
        && /only you know/.test(await need.textContent()), await row.textContent());

  await need.locator("[data-needset]").click();
  await p.waitForTimeout(500);
  check("saving nothing is refused", /Type the value first/.test(await toast()), await toast());
  await need.locator("[data-needvalue]").fill("<still a placeholder>");
  await need.locator("[data-needset]").click();
  await p.waitForTimeout(500);
  check("so is another placeholder", /still looks like a placeholder/.test(await toast()), await toast());
  check("and the file was not touched",
        JSON.parse(fs.readFileSync(FILE, "utf8")).scenarios[0].data.probeKey === "<a real key only you know>");

  await need.locator("[data-needvalue]").fill("K-2291");
  await need.locator("[data-needset]").click();
  await p.waitForFunction(() => /saved/i.test(document.querySelector("#toast").textContent),
                          null, { timeout: 10000 }).catch(() => {});
  check("a real value is saved, and the screen says so", /probeKey saved/.test(await toast()), await toast());
  check("it is written to the test's own file",
        JSON.parse(fs.readFileSync(FILE, "utf8")).scenarios[0].data.probeKey === "K-2291");
  await p.waitForTimeout(800);
  const after = p.locator("#libList .librow").first();
  check("the flag is gone", !/needs a value/.test(await after.locator(".top .libres").textContent()),
        await after.locator(".top .libres").textContent());
  check("and so is the box asking for it", await after.locator("[data-libneed]").count() === 0);

  await after.locator("[data-librun]").click();
  await p.waitForFunction(() => /passes|does not pass/.test(
    (document.querySelector("#libList .librow .top .libres") || {}).textContent || ""), null, { timeout: 60000 }).catch(() => {});
  check("with the value in place the test runs and passes",
        /passes/.test(await p.locator("#libList .librow .top .libres").first().textContent()),
        await p.locator("#libList .librow").first().textContent());

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
