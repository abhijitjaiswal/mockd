/**
 * workbench.js — assembling a dependent flow from real responses.
 *
 * The thing being proven: you can build a two-step flow where the second step
 * needs an id the first one produces, WITHOUT knowing in advance that the
 * response says `data.id`. Run step one, click the value, bind it, run the
 * flow. That was the missing half — the model always supported the chaining.
 */
const { chromium } = require("playwright");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

const COLLECTION = process.env.MOCKD_COLLECTION
  || "/api/v1/recruitment-settings/positions/departments";
const NESTED = process.env.MOCKD_NESTED
  || "/api/v1/recruitment-settings/positions/departments/{{departmentId}}/levels";

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error" && !/status of 4\d\d/.test(m.text()))
                             errs.push("console: " + m.text()); });
  // the binding name is confirmed through a prompt; accept what is proposed
  let proposed = null;
  p.on("dialog", async (d) => { proposed = d.defaultValue(); await d.accept(d.defaultValue()); });

  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(1800);
  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForTimeout(1200);

  check("the workbench is on the Tests view", await p.locator("#wbCard").count() === 1);
  check("it starts closed", await p.locator("#wbBody").isHidden());
  await p.locator("#wbToggle").click();
  await p.waitForTimeout(900);
  check("opening it shows one empty step", await p.locator(".wbstep").count() === 1);
  check("levels come from the taxonomy",
        await p.locator("#wbLevel option").count() >= 5);
  check("it defaults to the mock", (await p.locator("#wbEnv").inputValue()) === "mock");
  check("and says so", (await p.locator("#wbTarget").textContent()).trim() === "mock");
  check("no live warning against the mock", await p.locator("#wbLiveWarn").isHidden());

  // a caution about where requests go is not a failure, and must not look like one
  const live = await p.evaluate(async () => {
    const d = await (await fetch("/api/environments")).json();
    return (d.environments || []).find((e) => !e.name.startsWith("mock"))?.name;
  });
  if (live) {
    await p.locator("#wbEnv").selectOption(live);
    await p.waitForTimeout(1200);
    check("a real target is called out", !(await p.locator("#wbLiveWarn").isHidden()));
    check("as a caution, not an error",
          (await p.locator("#wbLiveWarn").getAttribute("class")).includes("warn"),
          await p.locator("#wbLiveWarn").getAttribute("class"));
    const colour = await p.locator("#wbLiveWarn").evaluate(
      (el) => getComputedStyle(el).color);
    const errColour = await p.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue("--err").trim());
    check("and it is not painted in the error colour",
          !colour.includes(errColour), `${colour} vs ${errColour}`);
    if (await p.locator("#envDlg").isVisible()) await p.locator("#envDlgCancel").click();
    await p.locator("#wbEnv").selectOption("mock");
    await p.waitForTimeout(800);
  }

  // --- step 1: create something ---
  await p.locator('[data-method="0"]').selectOption("POST");
  await p.locator('[data-path="0"]').fill(COLLECTION);
  await p.locator('[data-name="0"]').fill("create a department");
  await p.locator('[data-body="0"]').fill('{"title": "wb-{{$uuid}}", "description": "x"}');
  await p.locator('[data-run="0"]').click();
  await p.waitForTimeout(3000);

  check("step 1 ran and is marked passed",
        await p.locator(".wbstep.ran-pass").count() >= 1);
  const fields = await p.locator(".wbfield").count();
  check("the real response is offered field by field", fields > 0, String(fields));
  const firstField = await p.locator(".wbfield").first().textContent();
  check("ids are offered first", /ID/.test(firstField), firstField.slice(0, 70));

  // --- a list must not look like it held one thing ---
  await p.locator("#wbAdd").click(); await p.waitForTimeout(500);
  const listOp = await p.evaluate(async () => {
    const d = await (await fetch("/api/routes")).json();
    const r = (d.routes || d).find((x) => x.method === "GET" && /list|countries|cities/i.test(x.path)
                                          && !/\{/.test(x.path));
    return r ? r.path : "";
  });
  if (listOp) {
    await p.locator('[data-method="1"]').selectOption("GET");
    await p.waitForTimeout(500);
    await p.locator('[data-path="1"]').fill(listOp);
    await p.locator('[data-run="1"]').click();
    await p.waitForTimeout(3000);
    const panel = await p.locator('.wbstep[data-i="1"]').textContent();
    check("a list says how many items it held", /holds \d+ item/.test(panel),
          panel.slice(0, 140));
    check("and its length can be bound", /\.length/.test(panel));
    check("and it says the fields are from the first item",
          /from the first one/.test(panel));
    check("the whole response is available, not just the first item",
          /The whole response/.test(panel));
  }
  await p.locator('[data-del="1"]').click(); await p.waitForTimeout(500);

  // --- bind the id, without ever typing a JSON path ---
  await p.locator(".wbfield [data-bind]").first().click();
  await p.waitForTimeout(900);
  check("the binding name was proposed, not demanded",
        !!proposed && /Id$/.test(proposed), String(proposed));
  check("step 1 now provides it",
        (await p.locator('.wbstep[data-i="0"] .wbchip.out').count()) === 1);

  // --- step 2: consume it ---
  await p.locator("#wbAdd").click(); await p.waitForTimeout(600);
  check("a second step appears", await p.locator(".wbstep").count() === 2);
  await p.locator('[data-method="1"]').selectOption("POST");
  const nested = NESTED.replace("{{departmentId}}", `{{${proposed}}}`);
  await p.locator('[data-path="1"]').fill(nested);
  await p.locator('[data-path="1"]').dispatchEvent("change");
  await p.locator('[data-name="1"]').fill("add a level under it");
  await p.waitForTimeout(600);

  const wire = await p.locator('.wbstep[data-i="1"] .wbwire').textContent();
  check("step 2 shows what it consumes", wire.includes(proposed), wire.slice(0, 90));
  check("and it is not flagged missing",
        (await p.locator('.wbstep[data-i="1"] .wbchip.missing').count()) === 0);

  // --- an unknown variable must be visibly wrong before it is run ---
  await p.locator('[data-path="1"]').fill(nested.replace(`{{${proposed}}}`, "{{nothingProvidesThis}}"));
  await p.locator('[data-path="1"]').dispatchEvent("change");
  await p.waitForTimeout(600);
  check("a variable nothing provides is flagged before running",
        (await p.locator('.wbstep[data-i="1"] .wbchip.missing').count()) === 1);
  await p.locator('[data-path="1"]').fill(nested);
  await p.locator('[data-path="1"]').dispatchEvent("change");
  await p.waitForTimeout(500);

  // --- run the flow: step 2 must succeed using step 1's id ---
  await p.locator('[data-body="1"]').fill('{"title": "Senior", "level_order": 1}');
  await p.locator("#wbRunAll").click();
  await p.waitForTimeout(3000);
  check("the whole flow passes, second step using the captured id",
        (await p.locator(".wbstep.ran-pass").count()) === 2,
        await p.locator("#toast").textContent());

  // --- cleanup must be offered, not remembered ---
  check("a creating step offers a cleanup",
        await p.locator('[data-cleanup="0"]').count() === 1);
  await p.locator('[data-cleanup="0"]').click();
  await p.waitForTimeout(1800);
  check("the cleanup step is added", await p.locator(".wbstep").count() === 3);
  const cleanupPath = await p.locator('[data-path="2"]').inputValue();
  check("and it deletes what step 1 created",
        /DELETE/.test(await p.locator('[data-method="2"]').inputValue())
        && cleanupPath.includes(proposed), `${cleanupPath}`);

  // --- save it ---
  const id = "wb-probe-" + Date.now().toString(36);
  await p.locator("#wbModule").fill("workbench-probe");
  await p.locator("#wbId").fill(id);
  await p.locator("#wbLevel").selectOption("regression");
  await p.locator("#wbSave").click();
  await p.waitForTimeout(2000);
  check("saving reports where it went",
        /workbench-probe/.test(await p.locator("#wbSaveNote").textContent()),
        await p.locator("#wbSaveNote").textContent());

  const saved = await p.evaluate(async () => (await (await fetch("/api/tests")).json()));
  const suite = (saved.suites || []).find((s) => s.name === "workbench-probe");
  check("the flow is on disk as a scenario", !!suite && (suite.scenarios || []).length >= 1,
        JSON.stringify(suite || {}).slice(0, 90));
  const flow = suite && (suite.scenarios || [])[0];
  check("with both steps", flow && (flow.steps === 2 || flow.steps?.length === 2),
        JSON.stringify(flow || {}).slice(0, 110));

  const onDisk = await p.evaluate(async (args) => {
    const q = new URLSearchParams({ suite: args.suite, id: args.id, stage: "draft" });
    return await (await fetch("/api/tests/one?" + q)).json();
  }, { suite: "workbench-probe", id });
  const def = onDisk.test || onDisk;
  check("cleanup is stored apart from the steps, so it runs even on failure",
        Array.isArray(def.cleanup) && def.cleanup.length === 1,
        JSON.stringify(def).slice(0, 140));
  check("and the flow itself keeps only its two steps",
        (def.steps || []).length === 2, String((def.steps || []).length));

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
