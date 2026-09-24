/**
 * scoped.js — running one section, or one test, against a chosen server.
 *
 * The Sections panel could show tests and not run them: the only Run was "run
 * everything", and the only server it reached was the mock. A section you
 * cannot run against staging is a section you cannot trust against staging.
 */
const { chromium } = require("playwright");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error" && !/status of 4\d\d/.test(m.text()))
                             errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(1600);
  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForTimeout(1600);

  check("a section can be run", await p.locator('[data-act="run-suite"]').count() > 0);
  check("and so can a single test", await p.locator('[data-act="run"]').count() > 0);

  // the environment chosen on the left is the one used
  const envs = await p.locator("#testEnv option").allTextContents();
  check("the environment is selectable", envs.length > 1, envs.join(" | "));

  const before = await p.locator("#testOutCard").isHidden();
  const first = p.locator('[data-act="run"]').first();
  const id = await first.getAttribute("data-id");
  await first.click();
  await p.waitForTimeout(9000);

  check("running one test shows a result", !(await p.locator("#testOutCard").isHidden()),
        `was hidden: ${before}`);
  const head = await p.locator("#testOutHead").textContent();
  check("the report names what ran", head.includes(id), head.slice(0, 110));
  check("and which environment it ran against", /on mock/.test(head), head.slice(0, 110));
  check("and how it went", /pass|fail|blocked/.test(head), head.slice(0, 110));

  // one test means one test, not its neighbours
  const rows = await p.locator("#testRows .runrow, #testRows > *").count();
  check("only the chosen test ran", rows <= 3, `${rows} rows`);

  // a whole section
  await p.locator('[data-act="run-suite"]').first().click();
  await p.waitForTimeout(12000);
  const sectionHead = await p.locator("#testOutHead").textContent();
  check("running a section reports on the section",
        /section /.test(sectionHead), sectionHead.slice(0, 110));

  // Whichever button you press, the badges must end up telling the truth.
  // Only the scoped run refreshed them, so a full run left every badge showing
  // whatever it said when the list was last drawn.
  const badgeFor = async () => await p.evaluate(() => {
    const el = document.querySelector("#testTree .outcome");
    return el ? el.textContent.trim() : "";
  });
  await p.evaluate(async () => {
    // make the stored history disagree with what is on screen
    const el = document.querySelector("#testTree .outcome");
    if (el) el.textContent = "stale-marker";
  });
  check("the list can be made stale", (await badgeFor()) === "stale-marker");

  await p.locator("#btnRunTests").click();
  await p.waitForTimeout(20000);
  const after = await badgeFor();
  check("a full run redraws the badges too", after !== "stale-marker", after);

  // the report must be colour-coded by outcome, not always green
  const cls = await p.locator("#testOutHead").getAttribute("class");
  check("the report is marked by outcome", /banner (ok|err|warn)/.test(cls), cls);

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
