/**
 * selectors.js — everything the runner can select must be selectable here.
 *
 * Two faults this pins. Filters existed only as CLI flags, so "smoke tests for
 * one module" was unreachable from the console — and the pipeline generator
 * read the free-text tag chips as if they were levels, which would have put a
 * tag where a level belongs. And an environment missing its values was
 * DISABLED, which is a dead end: the thing you need to do next is fill it in,
 * and a dead option gives you nowhere to do that.
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
  await p.waitForTimeout(1800);
  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForTimeout(1800);

  // ------------------------------------------------------------ selectors
  check("levels are selectable", await p.locator("#testLevels option").count() >= 5);
  check("modules are selectable", await p.locator("#testModules option").count() >= 3);
  check("a single test can be named", await p.locator("#testOnly").count() === 1);
  const levelText = await p.locator("#testLevels").textContent();
  check("each level says what it means and how many it holds",
        /smoke \(\d+\)/.test(levelText) && /seconds not minutes/.test(levelText),
        levelText.slice(0, 80));
  check("a level with no tests cannot be chosen",
        await p.locator("#testLevels option[disabled]").count() >= 1);

  // the selection must reach the runner
  await p.locator("#testLevels").selectOption(["smoke"]);
  await p.locator("#testModules").selectOption(["departments"]);
  await p.waitForTimeout(300);
  const built = await p.evaluate(async () => (await (await fetch("/api/tests/pipeline", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ env: "mock", levels: ["smoke"], modules: ["departments"],
                           format: "command", drafts: true }) })).json()));
  check("the pipeline carries the level and the module",
        /--level smoke/.test(built.command) && /--module departments/.test(built.command),
        built.command);

  await p.locator("#btnPipeline").click(); await p.waitForTimeout(400);
  await p.locator("#genPipeGo").click(); await p.waitForTimeout(1500);
  const meta = await p.locator("#genMeta").textContent();
  check("the pipeline reads the SAME selection as the run card",
        /departments/.test(meta) && /smoke/.test(meta), meta);
  check("and a tag is never mistaken for a level",
        !/\bgenerated\b|\bedge\b/.test(meta), meta);

  // ------------------------------------------------------ environments
  const envText = await p.locator("#testEnv").textContent();
  check("an environment needing values is still listed",
        /dev/.test(envText), envText.slice(0, 100));
  check("and says how many values it needs",
        /needs \d+ value/.test(envText), envText.slice(0, 120));
  const disabled = await p.locator("#testEnv option[disabled]").count();
  check("no environment is a dead option", disabled === 0, String(disabled));

  await p.locator('nav.side a[data-view="explore"]').click();
  await p.waitForTimeout(1200);
  check("the explorer does not disable them either",
        await p.locator("#exploreTarget option[disabled]").count() === 0);

  // choosing one that is not ready must lead somewhere
  const notReady = await p.evaluate(async () => {
    const d = await (await fetch("/api/environments")).json();
    return (d.environments || []).find((e) => !e.ready)?.name;
  });
  if (notReady) {
    await p.locator("#exploreTarget").selectOption(notReady);
    await p.waitForTimeout(1500);
    check("picking one that needs values opens Configure",
          await p.locator("#envDlg").isVisible(), notReady);
    if (await p.locator("#envDlg").isVisible()) await p.locator("#envDlgCancel").click();
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
