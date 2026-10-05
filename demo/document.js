/**
 * document.js — bringing in an API document, as somebody new would.
 *
 * The screen has one job for most people: show which document is in use, and
 * let them swap in another from a link or a file. The suite clicks through
 * that and checks nothing is replaced until the person has seen what was
 * found and said yes.
 *
 * The document it switches to is a byte-for-byte copy of the one in use, so
 * the baseline tests come out the same; mockd.json is put back exactly and the
 * probe files are removed.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const os = require("os");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const PROJECT_FILE = path.join(DIR, "mockd.json");
const BEFORE = fs.existsSync(PROJECT_FILE) ? fs.readFileSync(PROJECT_FILE) : null;
const STAMP = Date.now().toString(36);
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "mockd-doc-"));
const TINY = path.join(TMP, `probe-tiny-${STAMP}.json`);
const COPY = path.join(TMP, `probe-copy-${STAMP}.json`);
const JUNK = path.join(TMP, `probe-junk-${STAMP}.json`);
process.on("exit", () => {
  try {
    if (BEFORE === null) fs.rmSync(PROJECT_FILE, { force: true });
    else fs.writeFileSync(PROJECT_FILE, BEFORE);
  } catch { /* nothing to restore */ }
  for (const f of [TINY, COPY, JUNK].map((f) => path.join(DIR, "specs", path.basename(f)))) {
    try { fs.rmSync(f, { force: true }); } catch { /* gone */ }
  }
  try { fs.rmSync(TMP, { recursive: true, force: true }); } catch { /* gone */ }
});

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error" && !/status of 400/.test(m.text()))
                             errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  const toast = async () => ((await p.locator("#toast").textContent().catch(() => "")) || "").trim();
  const project = async () => p.evaluate(async () => (await fetch("/api/project")).json());

  const original = (await project()).spec;
  fs.writeFileSync(TINY, JSON.stringify({ openapi: "3.1.0",
    info: { title: "Probe API", version: "9.9" },
    paths: { "/things": { get: { responses: { 200: { description: "ok" } } },
                          post: { responses: { 201: { description: "made" } } } } } }));
  fs.writeFileSync(JUNK, "this is a shopping list, not an API document");
  fs.copyFileSync(path.join(DIR, original), COPY);

  let switched = false;
  try {
    await p.locator('nav.side a[data-view="source"]').click();
    await p.waitForFunction(() => /endpoint/.test(document.querySelector("#docNow").textContent),
                            null, { timeout: 20000 }).catch(() => {});

    // ----------------------------------------------- what a newcomer sees
    check("the screen is called API document",
          (await p.locator('.view[data-view="source"] h2').textContent()).trim() === "API document");
    const now = (await p.locator("#docNow").textContent()).replace(/\s+/g, " ");
    const count = Number((now.match(/(\d+) endpoints?/) || [])[1]);
    check("it says how big the document in use is", count > 0, now);
    check("and where it came from", now.includes(original), now);
    const visible = await p.evaluate(() => {
      const view = document.querySelector('.view[data-view="source"]');
      return [...view.querySelectorAll("*")].filter((el) => el.offsetParent !== null
        && el.children.length === 0).map((el) => el.textContent).join(" ");
    });
    const jargon = /spec\.lock\.json|mockd\.json|digest|candidate|content-addressed|pinned/i;
    check("without the tool's own vocabulary", !jargon.test(visible),
          (visible.match(jargon) || [""])[0]);
    check("the advanced tools are shut", await p.locator("#srcAdvWrap").isHidden());
    const controls = await p.evaluate(() =>
      [...document.querySelectorAll('.view[data-view="source"] button, '
        + '.view[data-view="source"] input, .view[data-view="source"] select, '
        + '.view[data-view="source"] textarea')].filter((el) => el.offsetParent !== null).length);
    check("and there are at most five controls", controls <= 5, String(controls));

    // ------------------------------------------------------ being wrong
    await p.locator("#docLoad").click();
    await p.waitForTimeout(300);
    check("Load with nothing given says what to do", /link or choose a file/i.test(await toast()),
          await toast());
    await p.locator("#docUrl").fill("api.example.com/openapi.json");
    await p.locator("#docLoad").click();
    await p.waitForTimeout(300);
    check("a link without http is explained", /http:\/\/ or https:\/\//.test(await toast()),
          await toast());
    await p.locator("#docUrl").fill("");
    await p.locator("#docFile").setInputFiles(JUNK);
    check("choosing a file names it", /probe-junk/.test(await p.locator("#docChosen").textContent()));
    await p.locator("#docLoad").click();
    await p.waitForFunction(() => /not an API document|Could not load/i.test(
      document.querySelector("#toast").textContent), null, { timeout: 15000 }).catch(() => {});
    check("a file that is not an API document is refused in plain words",
          /not an API document/i.test(await toast()), await toast());
    check("and nothing is offered to use", await p.locator("#docFound").isHidden());

    // --------------------------------------------- loading shows, only
    await p.locator("#docFile").setInputFiles(TINY);
    await p.locator("#docLoad").click();
    await p.waitForFunction(() => !document.querySelector("#docFound").hidden,
                            null, { timeout: 15000 }).catch(() => {});
    const found = (await p.locator("#docFound").textContent()).replace(/\s+/g, " ");
    check("Load shows what was found", /Probe API/.test(found) && /2 endpoints/.test(found), found);
    check("beside the size of the one in use", found.includes(`has ${count}`), found);
    check("and has not switched anything yet", (await project()).spec === original);
    await p.locator("#docCancel").click();
    await p.waitForTimeout(300);
    check("Cancel puts it away", await p.locator("#docFound").isHidden());
    check("with the project still on its document", (await project()).spec === original);

    // ---------------------------------------------------- what changed
    if (await p.locator("#docDiff").count()) {
      await p.locator("#docDiff").click();
      await p.waitForFunction(() => !document.querySelector("#candidateCard").hidden,
                              null, { timeout: 15000 }).catch(() => {});
      check("What changed? opens the comparison",
            await p.locator("#candidateCard").isVisible());
      await p.evaluate(() => window.showSrcAdvanced(false));
    }

    // ------------------------------------------------------- switching
    await p.locator("#docFile").setInputFiles(COPY);
    await p.locator("#docLoad").click();
    await p.waitForFunction(() => !!document.querySelector("#docUse"),
                            null, { timeout: 20000 }).catch(() => {});
    await p.locator("#docUse").click();
    switched = true;
    await p.waitForFunction((n) => document.querySelector("#docNow").textContent.includes(n),
                            path.basename(COPY), { timeout: 60000 }).catch(() => {});
    const after = (await p.locator("#docNow").textContent()).replace(/\s+/g, " ");
    check("Use this document switches to it", after.includes(path.basename(COPY)), after);
    await p.waitForFunction(() => /Now using/.test(document.querySelector("#toast").textContent),
                            null, { timeout: 10000 }).catch(() => {});
    check("and says so", /Now using this document/.test(await toast()), await toast());
    check("the project records the choice",
          (await project()).spec === `specs/${path.basename(COPY)}`);
    const state = await p.evaluate(async () => (await fetch("/api/state")).json());
    check("the mock was moved onto it",
          state.running === true && String((state.options || {}).spec).includes(path.basename(COPY)),
          JSON.stringify(state.options || {}).slice(0, 140));
    check("the offer is put away", await p.locator("#docFound").isHidden());
    // Restart reads the mock's own setting; left on the old document, one
    // press would quietly undo the switch
    check("and Restart would keep it there",
          (await p.locator("#spec").inputValue()).includes(path.basename(COPY)),
          await p.locator("#spec").inputValue());

    // -------------------------------------------------------- advanced
    await p.locator("#srcAdvToggle").click();
    await p.waitForTimeout(300);
    check("Advanced opens the technical tools", await p.locator("#projSpec").isVisible());
  } finally {
    if (switched) {
      await p.evaluate(async (spec) => (await fetch("/api/project", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ spec }) })).json(), original).catch(() => {});
      await p.waitForTimeout(1500);
    }
  }
  check("the project is back on its own document", (await project()).spec === original);

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
