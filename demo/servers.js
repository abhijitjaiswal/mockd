/**
 * servers.js — adding and checking a server, as somebody new would.
 *
 * A server is a name, an address, a way of signing in and whether it is safe
 * to write to. The suite clicks through exactly that and checks the screen
 * never makes a person read a variable name or a file name to get it done.
 *
 * It adds a server pointing at the local mock, so nothing outside this machine
 * is touched, and it puts environments.json and .env back byte for byte.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const KEEP = ["environments.json", ".env"].map((name) => {
  const file = path.join(DIR, name);
  return { file, before: fs.existsSync(file) ? fs.readFileSync(file) : null };
});
process.on("exit", () => {
  for (const { file, before } of KEEP) {
    try {
      if (before === null) fs.rmSync(file, { force: true });
      else fs.writeFileSync(file, before);
    } catch { /* nothing to restore */ }
  }
});

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  const toast = async () => ((await p.locator("#toast").textContent().catch(() => "")) || "").trim();

  await p.locator('nav.side a[data-view="environments"]').click();
  await p.waitForFunction(() => document.querySelectorAll("#srvList .srvrow").length > 0,
                          null, { timeout: 15000 }).catch(() => {});

  // ------------------------------------------------- what a newcomer sees
  check("the screen is called Servers",
        (await p.locator('.view[data-view="environments"] h2').textContent()).trim() === "Servers");
  check("it lists servers", await p.locator("#srvList .srvrow").count() >= 1);
  const visible = await p.evaluate(() => {
    const view = document.querySelector('.view[data-view="environments"]');
    return [...view.querySelectorAll("*")].filter((el) => el.offsetParent !== null
      && el.children.length === 0).map((el) => el.textContent).join(" ");
  });
  check("without a variable name in sight", !/[A-Z]{2,}_[A-Z_]{3,}/.test(visible),
        (visible.match(/[A-Z]{2,}_[A-Z_]{3,}/) || [""])[0]);
  check("or a file name, or the word auth",
        !/environments\.json|\.env\b|\bauth\b/i.test(visible),
        (visible.match(/environments\.json|\.env\b|\bauth\b/i) || [""])[0]);
  check("the mock is there, marked as built in",
        /built in/.test(await p.locator('.srvrow[data-srv="mock"]').textContent()));
  check("technical variants are kept out of the way",
        await p.locator('.srvrow[data-srv^="mock-"]').count() === 0);
  check("the advanced tools are shut", await p.locator("#envAdvWrap").isHidden());
  const controls = await p.evaluate(() =>
    [...document.querySelectorAll('.view[data-view="environments"] button, '
      + '.view[data-view="environments"] input, .view[data-view="environments"] select')]
      .filter((el) => el.offsetParent !== null && !el.closest(".srvrow")).length);
  check("and there are only two controls besides the rows", controls <= 2, String(controls));

  // ----------------------------------------------------- test connection
  await p.locator('.srvrow[data-srv="mock"] [data-srvtest]').click();
  await p.waitForFunction(() => /Connected|Could not connect/.test(
    (document.querySelector('.srvrow[data-srv="mock"] .srvstate') || {}).textContent || ""),
    null, { timeout: 30000 }).catch(() => {});
  check("Test connection on the mock says Connected",
        /Connected/.test(await p.locator('.srvrow[data-srv="mock"] .srvstate').textContent()),
        await p.locator('.srvrow[data-srv="mock"]').textContent());

  // --------------------------------------------------------- add a server
  await p.locator("#srvAdd").click();
  await p.waitForTimeout(300);
  check("Add a server opens a short form", await p.locator("#srvName").isVisible());
  check("that asks how you sign in, in words",
        /cookie from my browser/i.test(await p.locator("#srvForm").textContent())
        && /username and password/i.test(await p.locator("#srvForm").textContent()));

  await p.locator("#srvName").fill("My Dev!");
  await p.locator("#srvUrl").fill("http://localhost:4010");
  await p.locator("#srvCreate").click();
  await p.waitForTimeout(400);
  check("a name it cannot use is explained", /short name/i.test(await toast()), await toast());

  const NAME = "probe-" + Date.now().toString(36);
  await p.locator("#srvName").fill(NAME);
  await p.locator("#srvUrl").fill("localhost:4010");
  await p.locator("#srvCreate").click();
  await p.waitForTimeout(400);
  check("an address without http is explained", /http:\/\/ or https:\/\//.test(await toast()), await toast());

  await p.locator('input[name="srvMode"][value="login"]').check();
  await p.waitForTimeout(200);
  check("choosing username and password asks for exactly those",
        await p.locator('#srvSecrets [data-secret="USERNAME"]').isVisible()
        && await p.locator('#srvSecrets [data-secret="PASSWORD"]').isVisible());
  await p.locator('input[name="srvMode"][value="none"]').check();
  await p.waitForTimeout(200);
  check("choosing no sign-in asks for nothing more",
        await p.locator("#srvSecrets [data-secret]").count() === 0);

  await p.locator("#srvUrl").fill("http://localhost:4010");
  await p.locator("#srvReadonly").check();
  await p.locator("#srvCreate").click();
  // "Ready" shows the instant it is saved; the test that follows takes a moment
  await p.waitForFunction((n) => /Connected|Could not connect/.test(
    (document.querySelector(`.srvrow[data-srv="${n}"] .srvstate`) || {}).textContent || ""),
    NAME, { timeout: 40000 }).catch(() => {});
  const mine = p.locator(`.srvrow[data-srv="${NAME}"]`);
  check("the new server appears in the list", await mine.count() === 1, await toast());
  check("it was tested straight away and connected",
        /Connected/.test(await mine.locator(".srvstate").textContent()),
        (await mine.textContent()).replace(/\s+/g, " ").trim());
  check("it is marked read only", /read only/.test(await mine.textContent()));
  check("and shows its address", /localhost:4010/.test(await mine.textContent()));

  // it can be picked on the Tests screen
  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForTimeout(1500);
  check("the new server can be chosen to run tests on",
        await p.locator(`#libEnv option[value="${NAME}"]`).count() === 1);

  // ------------------------------------------------------------- editing
  await p.locator('nav.side a[data-view="environments"]').click();
  await p.waitForTimeout(1200);
  await p.locator(`.srvrow[data-srv="${NAME}"] [data-srvedit]`).click();
  await p.waitForFunction((n) => !!document.querySelector(
    `.srvrow[data-srv="${n}"] [data-srvsave]`), NAME, { timeout: 15000 }).catch(() => {});
  const form = p.locator(`.srvrow[data-srv="${NAME}"] .srvform`);
  check("Edit shows its settings by plain name",
        /Address/.test(await form.textContent()), await form.textContent());
  check("and says what is already set",
        /already set/.test(await form.textContent()));
  await form.locator("[data-srvcancel]").click();
  await p.waitForTimeout(800);
  check("Cancel closes it without changing anything",
        await p.locator(`.srvrow[data-srv="${NAME}"] .srvform`).count() === 0);

  // ------------------------------------------------------------ advanced
  await p.locator("#envAdvToggle").click();
  await p.waitForTimeout(300);
  check("Advanced opens the technical tools", await p.locator("#envList").isVisible());
  check("where the technical servers are listed",
        /mock-auth|mock-login/.test(await p.locator("#envList").textContent()));

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
