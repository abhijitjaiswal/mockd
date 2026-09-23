/**
 * e2e.js — click through every section of the console the way a person would,
 * and assert the things are actually wired to each other.
 */
const { chromium } = require("playwright");
const EP = require("./endpoints");

let pass = 0, fail = 0;
const errs = [];
function check(name, ok, detail) {
  (ok ? pass++ : fail++);
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${name}${ok || !detail ? "" : "  — " + detail}`);
}

(async () => {
  const b = await chromium.launch();
  const c = await b.newContext({ viewport: { width: 1500, height: 1000 }, colorScheme: "light" });
  const p = await c.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });

  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(2500);

  // ---------------------------------------------------------------- shell
  console.log("\nSHELL");
  check("mock reported running", (await p.locator("#pilltext").textContent()).trim() === "running");
  check("base URL in the top bar", (await p.locator("#baseurl").textContent()).includes(":4010"));
  for (const v of ["overview", "authoring", "source", "connect", "explore", "tests",
                   "environments", "server"]) {
    await p.locator(`nav.side a[data-view="${v}"]`).click();
    await p.waitForTimeout(600);
    check(`view ${v} opens`, await p.locator(`.view[data-view="${v}"].on`).count() === 1);
  }

  // ---------------------------------------------------------------- overview
  console.log("\nOVERVIEW");
  await p.locator('nav.side a[data-view="overview"]').click(); await p.waitForTimeout(800);
  const tiles = await p.locator("#covBody .tile .n").allTextContents();
  check("coverage tiles rendered", tiles.length >= 3, tiles.join("/"));
  check("nav shows safe-to-build count",
        /\d+\/\d+/.test(await p.locator("#navCov").textContent()));
  await p.locator("#cov details summary").first().click(); await p.waitForTimeout(400);
  check("gap list expands", await p.locator("#cov details .oplist .op").count() > 0);

  // ---------------------------------------------------------------- authoring
  console.log("\nAUTHORING");
  await p.locator('nav.side a[data-view="authoring"]').click(); await p.waitForTimeout(1200);
  check("rules graded", await p.locator("#ruleBody .rule").count() >= 8);
  check("score in nav", /%/.test(await p.locator("#navScore").textContent()));
  check("guide text loaded", (await p.locator("#guideText").textContent()).includes("R1"));

  // ---------------------------------------------------------------- source
  console.log("\nSOURCE");
  await p.locator('nav.side a[data-view="source"]').click(); await p.waitForTimeout(1200);
  const statusText = await p.locator("#specStatus").textContent();
  // which status is correct depends on whether the project spec happens to be
  // the pinned one, so assert that provenance is REPORTED, not that it matches
  check("spec status rendered",
        /Matches the lock|Does NOT match|No spec\.lock\.json/.test(statusText),
        statusText.slice(0, 80));
  check("pin button present", await p.locator("#btnLockThis").count() === 1);

  // ---------------------------------------------------------------- connect
  console.log("\nCONNECT");
  await p.locator('nav.side a[data-view="connect"]').click(); await p.waitForTimeout(900);
  check("connect panel visible", await p.locator("#integ").isVisible());
  check("base URL shown", (await p.locator("#integBody").textContent()).includes("4010"));
  check("snippets present", await p.locator("#integBody .snip").count() >= 4);

  // ---------------------------------------------------------------- explore
  console.log("\nEXPLORE");
  await p.locator('nav.side a[data-view="explore"]').click(); await p.waitForTimeout(1200);
  const opCount = await p.locator("#ops .op").count();
  check("operations listed", opCount > 50, String(opCount));
  const targets = await p.locator("#exploreTarget option").allTextContents();
  check("target selector offers mock + envs", targets.length >= 2, targets.join(" | "));
  check("target defaults to mock", (await p.locator("#exploreTarget").inputValue()) === "mock");

  // click an operation -> fills the form
  await p.locator("#ops .op").nth(5).click(); await p.waitForTimeout(500);
  check("clicking an operation fills the path",
        (await p.locator("#path").inputValue()).startsWith("/api/"));

  await p.locator("#method").selectOption("GET");
  await p.locator("#path").fill(EP.COLLECTION);
  await p.locator("#btnSend").click(); await p.waitForTimeout(1500);
  check("send returns 200", (await p.locator("#respHead").textContent()).includes("200"));
  check("response labelled with target",
        (await p.locator("#respHead").textContent()).includes("mock"));
  check("body rendered", (await p.locator("#resp").textContent()).includes("data"));

  // force a 500
  await p.locator('button[data-h="X-Mock-Status: 500"]').click();
  await p.locator("#btnSend").click(); await p.waitForTimeout(1200);
  check("forced 500 works", (await p.locator("#respHead").textContent()).includes("500"));
  await p.locator("#clearHeaders").click();

  // scenario
  await p.locator('button[data-h="X-Mock-Scenario: empty"]').click();
  await p.locator("#btnSend").click(); await p.waitForTimeout(1200);
  check("empty scenario works",
        (await p.locator("#resp").textContent()).includes("No records found"));
  await p.locator("#clearHeaders").click();
  await p.locator("#btnSend").click(); await p.waitForTimeout(1200);

  // sample body for a POST
  await p.locator("#method").selectOption("POST");
  await p.locator("#path").fill(EP.COLLECTION);
  await p.locator("#btnSample").click(); await p.waitForTimeout(900);
  check("sample body filled", (await p.locator("#body").inputValue()).includes("title"));
  await p.locator("#btnBreak").click(); await p.waitForTimeout(400);
  await p.locator("#btnSend").click(); await p.waitForTimeout(1200);
  check("broken body rejected",
        /4\d\d/.test(await p.locator("#respHead").textContent()));

  // filters
  await p.locator('#collHint .filterbtn[data-state="complete"]').click();
  await p.waitForTimeout(500);
  const filtered = await p.locator("#ops .op").count();
  check("filter narrows the list", filtered > 0 && filtered < opCount,
        `${filtered} of ${opCount}`);
  await p.locator('#collHint .filterbtn[data-state=""]').click(); await p.waitForTimeout(400);

  // ---------------------------------------------------------------- save a test
  console.log("\nSAVE AS TEST");
  await p.locator("#method").selectOption("GET");
  await p.locator("#path").fill(EP.LIST);
  await p.locator("#body").fill("");
  await p.locator("#btnSend").click(); await p.waitForTimeout(1200);
  await p.locator("#btnSaveTest").click(); await p.waitForTimeout(1200);
  check("section defaults to the operation's tag",
        (await p.locator("#dlgSuite").inputValue()) === "user");
  check("destination shown",
        (await p.locator("#dlgWhere").textContent()).includes("tests/drafts/user.json"));
  check("assertions suggested", await p.locator("#dlgAsserts .arow2").count() > 3);
  await p.locator("#dlgId").fill("e2e-probe-case");
  await p.locator("#dlgSave").click(); await p.waitForTimeout(1500);
  check("save closed the dialog", !(await p.locator("#saveDlg").isVisible()));

  // ---------------------------------------------------------------- tests
  console.log("\nTESTS");
  await p.locator('nav.side a[data-view="tests"]').click(); await p.waitForTimeout(1500);
  check("sections rendered", await p.locator("#testTree .suite").count() > 0);
  check("saved test appears",
        (await p.locator("#testTree").textContent()).includes("e2e-probe-case")
        || (await p.locator("#testTree").textContent()).includes("List Users"));
  check("outcome badges present", await p.locator("#testTree .outcome").count() > 0);
  check("env dropdown populated", await p.locator("#testEnv option").count() > 0);
  const tagChips = await p.locator("#tagFilter .chip").count();
  check("label filter chips", tagChips > 1, String(tagChips));

  await p.locator("#btnRunTests").click(); await p.waitForTimeout(9000);
  check("run produced output",
        (await p.locator("#testOut").textContent()).includes("test(s)"));
  check("result header names the environment",
        (await p.locator("#testOutHead").textContent()).includes("environment"));
  check("result header names the spec digest",
        (await p.locator("#testOutHead").textContent()).includes("spec"));

  // ---------------------------------------------------------------- environments
  console.log("\nENVIRONMENTS");
  await p.locator('nav.side a[data-view="environments"]').click(); await p.waitForTimeout(1200);
  check("environment table rendered", await p.locator("#envList tr").count() > 2);
  check("live-check env dropdown", await p.locator("#liveEnv option").count() > 1);
  await p.locator("#liveUrl").fill("http://127.0.0.1:4010");
  await p.locator("#liveAuthMode").selectOption("bearer"); await p.waitForTimeout(300);
  await p.locator("#liveToken").fill("probe-token");
  await p.locator("#liveOnly").fill(`GET ${EP.LIST}$`);
  await p.locator("#btnVerifyLive").click(); await p.waitForTimeout(9000);
  check("live result stayed on this view",
        await p.locator('.view[data-view="environments"].on').count() === 1);
  check("live result rendered",
        (await p.locator("#liveOut").textContent()).includes("LIVE CHECK"));
  check("live result names auth used",
        (await p.locator("#liveOutHead").textContent()).includes("token"));

  // ---------------------------------------------------------------- server
  console.log("\nSERVER");
  await p.locator('nav.side a[data-view="server"]').click(); await p.waitForTimeout(900);
  check("stdout shown", (await p.locator("#stdout").textContent()).includes("routes"));
  check("drift panel filled", (await p.locator("#drift").textContent()).includes("operations"));

  console.log("\n" + "=".repeat(60));
  console.log(`${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close();
  process.exit(fail || errs.length ? 1 : 0);
})();
