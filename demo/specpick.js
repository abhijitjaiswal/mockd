/**
 * specpick.js — choosing which document the mock serves.
 *
 * Three faults this pins down, all of which made a saved spec look lost:
 *   - the list came from the project root only, so specs/ (where every fetch
 *     and upload lands) was never offered;
 *   - it was filtered by file extension, so environments.json and
 *     spec.lock.json were offered as if they were servable documents;
 *   - the control was a <datalist>, which shows nothing until you already know
 *     what to type — so there was no way to browse at all.
 * And the choice is now remembered, which is what "make it the default" means.
 */
const { chromium } = require("playwright");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(2000);
  await p.locator('nav.side a[data-view="server"]').click();
  await p.waitForTimeout(700);

  check("the picker is a visible control", await p.locator("#specPick").isVisible());

  const opts = await p.locator("#specPick option").allTextContents();
  check("it lists documents from the project root", opts.includes("apis.json"), opts.join(" | "));
  check("and documents under specs/",
        opts.some((o) => o.startsWith("specs/")), opts.join(" | "));
  check("non-specs are not offered as specs",
        !opts.includes("environments.json") && !opts.includes("spec.lock.json"),
        opts.join(" | "));
  check("there is an escape hatch for a path or URL",
        opts.some((o) => /Another file or a URL/.test(o)), opts.join(" | "));

  // picking drives the field the rest of the console reads
  await p.locator("#specPick").selectOption("apis.json");
  await p.waitForTimeout(500);
  check("picking sets the spec", (await p.locator("#spec").inputValue()) === "apis.json");
  check("and hides the free-text path", await p.locator("#spec").isHidden());

  await p.locator("#specPick").selectOption("__custom__");
  await p.waitForTimeout(400);
  check("choosing custom reveals the path field", await p.locator("#spec").isVisible());

  // typing a known name snaps the picker back to it
  await p.locator("#spec").fill("apis.json");
  await p.locator("#spec").dispatchEvent("change");
  await p.waitForTimeout(300);
  check("typing a known spec re-selects it",
        (await p.locator("#specPick").inputValue()) === "apis.json");

  // the remembered default survives a reload
  const remembered = await p.evaluate(async () =>
    (await (await fetch("/api/defaults")).json()).remembered.spec);
  check("a started spec is remembered", !!remembered, String(remembered));
  await p.reload({ waitUntil: "networkidle" });
  await p.waitForTimeout(1800);
  await p.locator('nav.side a[data-view="server"]').click();
  await p.waitForTimeout(600);
  check("and is pre-selected after a reload",
        (await p.locator("#spec").inputValue()) === remembered,
        `${await p.locator("#spec").inputValue()} vs ${remembered}`);
  check("with the hint naming it",
        (await p.locator("#specDefaultHint").textContent()).includes(remembered));

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
