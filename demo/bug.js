/**
 * bug.js — a failed test, turned into a report by one press.
 *
 * Writing a bug by hand means copying a request, a response and a guess at
 * whose fault it is out of three places. This clicks the button that does it
 * and reads what lands on the clipboard: it has to be enough for a developer
 * to reproduce the failure without asking anything, and it must never contain
 * a credential.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const FILE = path.join(DIR, "tests", "drafts", "bug-probe.json");
process.on("exit", () => { try { fs.rmSync(FILE, { force: true }); } catch { /* gone */ } });

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext();
  await ctx.grantPermissions(["clipboard-read", "clipboard-write"]);
  const p = await ctx.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });

  const stamp = Date.now().toString(36);
  const BAD = `bug-probe-bad-${stamp}`, GOOD = `bug-probe-good-${stamp}`;
  for (const [id, status] of [[BAD, 418], [GOOD, 200]]) {
    await p.evaluate(async ([id, status]) =>
      await fetch("/api/tests/save-flow", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ suite: "bug-probe", stage: "draft", flow: {
          id, name: `Quokka ${id}`, kind: "e2e", levels: ["smoke"], priority: "P1",
          links: ["ABC-77"], description: "The mock should say which routes it serves.",
          steps: [{ role: "target", name: "ask for the routes",
                    request: { method: "GET", path: "/_mock/routes" },
                    assertions: [{ type: "status", equals: status }] }] } }) }), [id, status]);
  }

  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForFunction(() => document.querySelectorAll("#libList .librow").length > 0,
                          null, { timeout: 15000 }).catch(() => {});
  await p.locator("#libSearch").fill("quokka");
  await p.waitForTimeout(500);
  check("both probe tests are listed", await p.locator("#libList .librow").count() === 2);

  await p.locator("#libRun").click();
  await p.waitForFunction((key) => {
    const r = document.querySelector(`.librow[data-lib="${key}"] .top .libres`);
    return r && /does not pass/.test(r.textContent);
  }, `bug-probe|draft|${BAD}`, { timeout: 60000 }).catch(() => {});

  const bad = p.locator(`.librow[data-lib="bug-probe|draft|${BAD}"]`);
  const good = p.locator(`.librow[data-lib="bug-probe|draft|${GOOD}"]`);
  check("the failing one is marked as not passing",
        /does not pass/.test(await bad.locator(".top .libres").textContent()));

  await good.locator(".top .nm").click();
  await p.waitForTimeout(300);
  check("a passing test offers no bug report — there is no bug",
        await good.locator('[data-libact="bug"]').count() === 0);

  await bad.locator(".top .nm").click();
  await p.waitForTimeout(300);
  const badRow = p.locator(`.librow[data-lib="bug-probe|draft|${BAD}"]`);
  check("a failing test offers one", await badRow.locator('[data-libact="bug"]').isVisible());

  await badRow.locator('[data-libact="bug"]').click();
  await p.waitForTimeout(1200);
  const clip = await p.evaluate(() => navigator.clipboard.readText());
  check("pressing it puts a report on the clipboard", clip.length > 200, String(clip.length));
  check("the report is titled with the test and the server",
        new RegExp(`Quokka ${BAD}`).test(clip) && /fails on mock/.test(clip), clip.slice(0, 120));
  check("it says what was expected and what happened",
        /\*\*Expected:\*\*.*418/.test(clip) && /\*\*Actual:\*\*/.test(clip));
  check("it carries a curl to reproduce it", /curl -X GET '.*\/_mock\/routes'/.test(clip));
  check("and the response that came back", /\*\*Response\*\* \(status 200\)/.test(clip));
  check("the linked ticket and priority travel with it",
        /ABC-77/.test(clip) && /priority P1/.test(clip));
  check("there is no credential in it",
        !/access_token|Authorization:|Cookie:/i.test(clip));
  check("what was copied is shown, in case the clipboard was blocked",
        (await badRow.locator(".bugtext").textContent()) === clip);
  check("and the message says what to do with it",
        /paste it into your tracker/i.test(await p.locator("#toast").textContent()));

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
