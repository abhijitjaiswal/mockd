/**
 * list.js — the Tests screen as a newcomer meets it.
 *
 * One list, a search box, four filters, a server picker and a Run button. The
 * suite clicks through exactly that, and also holds the screen to a budget:
 * with Advanced shut, there must be few enough controls to take in at a glance.
 * If a later change quietly adds a sixth dropdown, this is what objects.
 *
 * Works on a probe suite of its own and removes it afterwards.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const FILE = path.join(DIR, "tests", "drafts", "list-probe.json");
process.on("exit", () => { try { fs.rmSync(FILE, { force: true }); } catch { /* gone */ } });

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });

  const stamp = Date.now().toString(36);
  const ID = `list-probe-${stamp}`;
  const saved = await p.evaluate(async (id) =>
    await (await fetch("/api/tests/save-flow", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ suite: "list-probe", stage: "draft", flow: {
        id, name: `Zebra probe ${id}`, kind: "e2e", levels: ["smoke"], priority: "P2",
        description: "Asks the mock which routes it serves.",
        steps: [{ role: "target", name: "ask for the routes",
                  request: { method: "GET", path: "/_mock/routes" },
                  assertions: [{ type: "status", equals: 200 }] }] } }) })).json(), ID);
  check("a probe test exists", saved.ok === true, JSON.stringify(saved).slice(0, 100));

  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForFunction(() => document.querySelectorAll("#libList .librow").length > 0,
                          null, { timeout: 15000 }).catch(() => {});

  // ------------------------------------------------- what a newcomer sees
  check("the screen is a list of tests", await p.locator("#libList .librow").count() > 0);
  check("the advanced tools are shut", await p.locator("#advWrap").isHidden());
  check("so the workbench is not in the way", await p.locator("#wbCard").isHidden());
  check("nor the old run panel", await p.locator("#testLevels").isHidden());
  const controls = await p.evaluate(() =>
    [...document.querySelectorAll('.view[data-view="tests"] button, '
      + '.view[data-view="tests"] select, .view[data-view="tests"] input, '
      + '.view[data-view="tests"] textarea')]
      .filter((el) => el.offsetParent !== null && !el.closest(".librow")
                      && !el.closest("#libPager")).length);
  check("there are few enough controls to take in at a glance (12 or fewer)",
        controls <= 12, `${controls} visible controls`);
  check("it says how many tests there are",
        /\d+ tests?/.test(await p.locator("#libCount").textContent()));

  // ------------------------------------------------------------ finding
  await p.locator("#libSearch").fill("zebra probe");
  await p.waitForTimeout(400);
  check("typing a name finds the test", await p.locator("#libList .librow").count() === 1,
        String(await p.locator("#libList .librow").count()));
  check("the count follows", /1 of \d+/.test(await p.locator("#libCount").textContent()),
        await p.locator("#libCount").textContent());
  check("the Run button says how many it will run",
        /Run these 1/.test(await p.locator("#libRun").textContent()),
        await p.locator("#libRun").textContent());

  await p.locator("#libSearch").fill("");
  await p.locator("#libModule").selectOption("list-probe");
  await p.waitForTimeout(400);
  check("a module can be chosen", await p.locator("#libList .librow").count() === 1);
  await p.locator("#libPriority").selectOption("P0");
  await p.waitForTimeout(400);
  check("a filter that matches nothing says so plainly",
        /Nothing matches/.test(await p.locator("#libList").textContent()));
  await p.locator("#libPriority").selectOption("");
  await p.locator("#libResult").selectOption("never");
  await p.waitForTimeout(400);
  check("a test that has never run can be found as such",
        await p.locator("#libList .librow").count() === 1);
  await p.locator("#libResult").selectOption("");
  await p.waitForTimeout(300);

  // ---------------------------------------------------------- one test
  const row = p.locator(`.librow[data-lib="list-probe|draft|${ID}"]`);
  check("the row says the test has not run", /not run yet/.test(await row.textContent()));
  await row.locator(".top .nm").click();
  await p.waitForTimeout(300);
  check("clicking a test opens what it is for",
        /which routes it serves/.test(await row.locator(".more").textContent()));
  check("and its steps in words", /ask for the routes/.test(await row.locator(".more").textContent()));

  await row.locator('[data-libact="record"]').click();
  await p.waitForTimeout(300);
  check("details can be edited right there", await row.locator(".receditor").isVisible());
  await row.locator('.receditor [data-f="priority"]').selectOption("P0");
  await row.locator(".receditor [data-recsave]").click();
  await p.waitForTimeout(1500);
  const again = p.locator(`.librow[data-lib="list-probe|draft|${ID}"]`);
  check("and the change shows in the list",
        (await again.locator(".prio").first().textContent()).trim() === "P0");

  // ------------------------------------------------------------ running
  await again.locator("[data-librun]").click();
  await p.waitForFunction((key) => {
    const r = document.querySelector(`.librow[data-lib="${key}"] .libres`);
    return r && /passes|does not pass/.test(r.textContent);
  }, `list-probe|draft|${ID}`, { timeout: 60000 }).catch(() => {});
  const ran = p.locator(`.librow[data-lib="list-probe|draft|${ID}"]`);
  check("pressing Run on a test runs it and shows the result on its row",
        /passes/.test(await ran.locator(".top .libres").textContent()),
        await ran.locator(".top .libres").textContent());
  check("the full result is on the screen too", await p.locator("#testOutCard").isVisible());

  await p.locator("#libRun").click();
  await p.waitForFunction(() => /Run these|Run all/.test(
    document.querySelector("#libRun").textContent), null, { timeout: 60000 }).catch(() => {});
  check("Run these runs what the filters show",
        /list-probe|1 test/.test(await p.locator("#testOutHead").textContent())
        || /1 pass/.test(await p.locator("#testOutHead").textContent()),
        (await p.locator("#testOutHead").textContent()).slice(0, 100));

  // ----------------------------------------------------------- advanced
  await p.locator("#advToggle").click();
  await p.waitForTimeout(300);
  check("Advanced tools open when asked for", await p.locator("#wbCard").isVisible());
  await p.locator("#advToggle").click();
  await p.waitForTimeout(300);
  check("and shut again", await p.locator("#wbCard").isHidden());

  const target = p.locator(`.librow[data-lib="list-probe|draft|${ID}"]`);
  if (!(await target.locator(".more").count())) await target.locator(".top .nm").click();
  await target.locator('[data-libact="open"]').click();
  await p.waitForTimeout(1500);
  check("Edit steps opens the workbench with that test in it",
        await p.locator("#wbCard").isVisible()
        && (await p.locator("#wbId").inputValue()) === ID,
        await p.locator("#wbId").inputValue());

  // ----------------------------------------------------------- removing
  const last = p.locator(`.librow[data-lib="list-probe|draft|${ID}"]`);
  if (!(await last.locator(".more").count())) await last.locator(".top .nm").click();
  await last.locator('[data-libact="delete"]').click();
  await p.waitForTimeout(300);
  check("Remove asks once before doing it",
        /Really remove/.test(await last.locator('[data-libact="delete"]').textContent())
        && fs.existsSync(FILE));
  await last.locator('[data-libact="delete"]').click();
  await p.waitForTimeout(1500);
  check("and then it is gone from the list",
        await p.locator(`.librow[data-lib="list-probe|draft|${ID}"]`).count() === 0);

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
