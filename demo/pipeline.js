/**
 * pipeline.js — a selection becomes a pipeline, and a story becomes a brief.
 *
 * The point of generating CI from the console is that the pipeline runs the
 * SAME command you just ran. A pipeline that selects tests differently is one
 * whose failures you cannot reproduce locally, which is worse than no pipeline.
 */
const { chromium } = require("playwright");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

(async () => {
  const b = await chromium.launch();
  const c = await b.newContext({ permissions: ["clipboard-read", "clipboard-write"] });
  const p = await c.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error" && !/status of 4\d\d/.test(m.text()))
                             errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(1800);
  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForTimeout(1500);

  // ---------------------------------------------------------------- pipeline
  check("a pipeline can be generated from here", await p.locator("#btnPipeline").count() === 1);
  await p.locator("#btnPipeline").click(); await p.waitForTimeout(500);
  check("the panel opens", await p.locator("#genCard").isVisible());
  check("it asks which CI", await p.locator("#genPipeRow").isVisible());
  check("and does not ask for a story", await p.locator("#genStoryRow").isHidden());

  await p.locator("#genFormat").selectOption("github");
  await p.locator("#genPipeGo").click(); await p.waitForTimeout(1500);
  const yaml = await p.locator("#genOut").textContent();
  check("it emits a workflow", /jobs:/.test(yaml) && /actions\/checkout/.test(yaml),
        yaml.slice(0, 70));
  check("which runs tests.py", /python tests\.py run/.test(yaml));
  check("and publishes results", /results\.xml/.test(yaml));
  check("it says how much this selection runs",
        /test\(s\)/.test(await p.locator("#genMeta").textContent()),
        await p.locator("#genMeta").textContent());

  // the command it emits must be the one that actually runs
  const cmd = await p.evaluate(async () => (await (await fetch("/api/tests/pipeline", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ env: "mock", levels: ["smoke"], format: "command",
                           drafts: true }) })).json()));
  check("the command carries the selection",
        /--level smoke/.test(cmd.command) && /--env mock/.test(cmd.command), cmd.command);
  const preview = await p.evaluate(async () => (await (await fetch("/api/tests/pipeline", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ env: "mock", levels: ["smoke"], drafts: true }) })).json()));
  // how many smoke tests exist depends on what is saved, so ask
  const smokeCount = await p.evaluate(async () => {
    const d = await (await fetch("/api/tests/taxonomy")).json();
    return (d.levels || []).find((l) => l.name === "smoke")?.tests ?? -1;
  });
  check("and the preview count matches what that selection holds",
        preview.selects === smokeCount,
        `${preview.selects} vs ${smokeCount} smoke tests`);

  await p.locator("#genFormat").selectOption("gitlab");
  await p.locator("#genPipeGo").click(); await p.waitForTimeout(1200);
  check("GitLab is offered too",
        /artifacts:/.test(await p.locator("#genOut").textContent()));

  // ------------------------------------------------------------------ story
  await p.locator("#btnStory").click(); await p.waitForTimeout(500);
  check("the story panel opens", await p.locator("#genStoryRow").isVisible());
  check("and hides the CI picker", await p.locator("#genPipeRow").isHidden());
  await p.locator("#genStory").fill(
    "As an administrator I want to create a team and add approval levels to it, "
    + "so that requests raised under it follow the right approval chain.");
  await p.locator("#genStoryGo").click(); await p.waitForTimeout(2500);
  const brief = await p.locator("#genOut").textContent();
  check("the brief carries the story", /approval chain/.test(brief));
  check("it names operations from the spec, not invented ones",
        /OPERATIONS THAT LOOK RELEVANT/.test(brief) && /\/api\/v1\//.test(brief));
  check("it forbids inventing endpoints", /instead of inventing an endpoint/.test(brief));
  check("it demands non-constant data", /\{\{\$uuid\}\}|\{\{\$runId\}\}/.test(brief));
  check("it demands cleanup", /cleanup step/.test(brief));
  check("it demands a level", /give every test `levels`/.test(brief));
  check("it lists what is already covered", /ALREADY COVERED/.test(brief));

  await p.locator("#genCopy").click(); await p.waitForTimeout(600);
  const copied = await p.evaluate(() => navigator.clipboard.readText());
  check("the brief can be copied whole", copied.length > 400 && /THE STORY/.test(copied),
        String(copied.length));

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
