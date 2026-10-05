/**
 * baseline.js — the tests nobody had to write.
 *
 * A new person's first question is "does anything work?", and the honest answer
 * should take one button. So a baseline suite exists as soon as the mock is up
 * on the project spec: one check per integration point, the flows the paths
 * imply, and the refusals the document promises.
 *
 * The two things under test: it is there without being asked for, and
 * refreshing it never overwrites a test somebody has made their own.
 *
 * Works on the real baseline file, and puts it back exactly as it was.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const FILE = path.join(DIR, "tests", "drafts", "baseline.json");
const before = fs.existsSync(FILE) ? fs.readFileSync(FILE, "utf8") : null;
process.on("exit", () => {
  try {
    if (before === null) fs.rmSync(FILE, { force: true });
    else fs.writeFileSync(FILE, before);
  } catch { /* nothing to restore */ }
});

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });

  const post = (url, body) => p.evaluate(async ([u, bd]) =>
    await (await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify(bd) })).json(), [url, body]);
  const get = (url) => p.evaluate(async (u) => await (await fetch(u)).json(), url);

  // ------------------------------------------------- it is simply there
  check("a baseline exists without anyone asking for one", before !== null);
  const status = await get("/api/tests/baseline");
  check("the console knows about it", status.exists === true, JSON.stringify(status).slice(0, 120));
  check("it holds a test per integration point, and more", status.tests >= 10, String(status.tests));
  check("it says which document it was made from",
        !!(status.generated_for && status.generated_for.spec),
        JSON.stringify(status.generated_for));
  check("it is made of contract checks, refusals and flows",
        Object.keys(status.kinds || {}).length >= 2, JSON.stringify(status.kinds));

  // ------------------------------------------------------- on the screen
  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForTimeout(1500);
  check("the Tests screen offers it first", await p.locator("#baseCard").isVisible());
  check("and says how many are ready",
        /\d+ ready/.test(await p.locator("#baseCount").textContent()),
        await p.locator("#baseCount").textContent());
  check("with one button to run them", !(await p.locator("#baseRun").isDisabled()));

  // ------------------------------------- pressing the button, as a person
  // The first version of this suite ran the baseline through the API and never
  // clicked anything, and the button gave no sign of life where it was pressed.
  await p.locator("#baseRun").click();
  await p.waitForTimeout(400);
  check("pressing Run says so right where it was pressed",
        /running/i.test(await p.locator("#baseCount").textContent())
        || /Running the baseline/.test(await p.locator("#baseState").textContent()),
        await p.locator("#baseCount").textContent());
  await p.waitForFunction(
    () => /passed on/.test(document.querySelector("#baseCount").textContent),
    null, { timeout: 90000 }).catch(() => {});
  const shown = (await p.locator("#baseCount").textContent()).trim();
  check("and then how it went, on the card itself", /\d+ of \d+ passed on mock/.test(shown), shown);
  const [good, total] = (shown.match(/(\d+) of (\d+)/) || []).slice(1).map(Number);
  check("every baseline test passes against the mock", total > 0 && good === total, shown);
  check("the message at the top says the same",
        /Baseline:/.test(await p.locator("#toast").textContent()),
        (await p.locator("#toast").textContent()).trim().slice(0, 90));
  await p.waitForTimeout(1500);                    // the scroll is animated
  check("and the result is brought into view",
        await p.locator("#testOutCard").isVisible()
        && await p.evaluate(() => {
          const r = document.querySelector("#testOutCard").getBoundingClientRect();
          return r.top < window.innerHeight && r.bottom > 0; }));

  // --------------------------------------- refreshing changes nothing new
  const same = await post("/api/tests/baseline", {});
  check("refreshing an up-to-date baseline succeeds", same.ok === true,
        JSON.stringify(same).slice(0, 160));
  check("and rewrites nothing", same.written === false && same.added === 0
        && same.updated === 0, JSON.stringify({ w: same.written, a: same.added, u: same.updated }));

  // ------------------------------ a test somebody edited is theirs to keep
  const suite = JSON.parse(fs.readFileSync(FILE, "utf8"));
  const target = (suite.cases || [])[0];
  check("there is a test to make our own", !!target);
  const edit = await post("/api/tests/record", {
    suite: "baseline", stage: "draft", id: target.id,
    fields: { priority: "P3", owner: "sam", description: "ours now" } });
  check("its record can be edited", edit.ok === true, JSON.stringify(edit).slice(0, 120));

  // take an untouched one away, as if the suite were out of date, then refresh
  const doctored = JSON.parse(fs.readFileSync(FILE, "utf8"));
  const other = doctored.cases[1];
  doctored.cases.splice(1, 1);
  fs.writeFileSync(FILE, JSON.stringify(doctored, null, 2) + "\n");

  const refreshed = await post("/api/tests/baseline", {});
  check("refreshing after a change succeeds", refreshed.ok === true,
        JSON.stringify(refreshed).slice(0, 160));
  const now = JSON.parse(fs.readFileSync(FILE, "utf8"));
  const mine = now.cases.find((c) => c.id === target.id);
  check("the edited test kept its priority, owner and description",
        mine.priority === "P3" && mine.owner === "sam" && mine.description === "ours now",
        JSON.stringify({ p: mine.priority, o: mine.owner }));
  check("and the count says one was left alone", refreshed.kept_edited >= 1,
        String(refreshed.kept_edited));
  check("a test the document calls for, and the suite lacked, is made again",
        !!now.cases.find((c) => c.id === other.id) && refreshed.added >= 1,
        JSON.stringify({ added: refreshed.added }));

  // ------------------------------------------------------ and it runs
  const run = await post("/api/tests/run", {
    env: "mock", suite: "baseline", drafts: true, kinds: ["case"], levels: ["smoke"] });
  check("the baseline can be run", !!run.job, JSON.stringify(run).slice(0, 120));
  let done = false;
  for (let i = 0; i < 90 && !done; i++) {
    done = (await get(`/api/job/${run.job}`)).done;
    if (!done) await p.waitForTimeout(1000);
  }
  const report = await get(`/api/job/${run.job}/report`);
  const summary = report.summary || {};
  check("its contract checks pass against the mock",
        (summary.pass || 0) > 0 && !(summary.fail || 0) && !(summary.error || 0),
        JSON.stringify(summary));

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
