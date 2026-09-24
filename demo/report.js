/**
 * report.js — a run you can send to somebody.
 *
 * JUnit XML is for a CI server and JSON is for a program. Neither answers
 * "did it pass on staging?" from a person who will not be opening a terminal,
 * and a run that only exists in scrollback is a run that has to be repeated.
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
  await p.waitForTimeout(1600);

  check("a report can be downloaded", await p.locator("#testReport").count() === 1);

  await p.locator("#btnRunTests").click();
  await p.waitForTimeout(20000);

  // every run must leave all three behind, without being asked
  for (const kind of ["html", "xml", "json"]) {
    const got = await p.evaluate(async (k) => {
      const r = await fetch(`/api/tests/report.${k}`);
      return { status: r.status, body: (await r.text()).slice(0, 4000) };
    }, kind);
    check(`a ${kind} report exists after a run`, got.status === 200, String(got.status));
    if (kind === "html" && got.status === 200) {
      check("the report says what it ran against",
            /against/.test(got.body) && /mock|localhost/.test(got.body),
            got.body.slice(0, 120));
      check("and which document", /document/.test(got.body));
      check("and counts the outcomes",
            /passed/.test(got.body) && /failed/.test(got.body));
      check("it stands alone — no external stylesheet or script",
            !/<link[^>]+stylesheet|<script[^>]+src=/.test(got.body));
      check("and reads in a dark browser too",
            /prefers-color-scheme/.test(got.body));
    }
    if (kind === "xml" && got.status === 200) {
      check("the xml is JUnit a CI server will render",
            /<testsuite/.test(got.body), got.body.slice(0, 90));
    }
  }

  // --- the report must be THIS run's, not whatever finished last ---
  // Two runs, different selections. Each download must match the run that
  // asked for it; a single shared "last run" file meant the second overwrote
  // the first and you read somebody else's results as your own.
  const first = await p.evaluate(async () => {
    const r = await (await fetch("/api/tests/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ env: "mock", drafts: true, modules: ["departments"],
                             kinds: [] }) })).json();
    return r.job;
  });
  await p.waitForTimeout(12000);
  const second = await p.evaluate(async () => {
    const r = await (await fetch("/api/tests/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ env: "mock", drafts: true, modules: ["user"],
                             kinds: [] }) })).json();
    return r.job;
  });
  await p.waitForTimeout(12000);

  const firstReport = await p.evaluate(async (job) =>
    (await (await fetch(`/api/tests/report.json?job=${job}`)).text()).slice(0, 4000), first);
  const secondReport = await p.evaluate(async (job) =>
    (await (await fetch(`/api/tests/report.json?job=${job}`)).text()).slice(0, 4000), second);
  check("the earlier run still has its own report",
        /departments/.test(firstReport), firstReport.slice(0, 90));
  check("and it was not overwritten by the later one",
        !/"name": "user"/.test(firstReport), firstReport.slice(0, 90));
  check("the later run has its own",
        /user/.test(secondReport), secondReport.slice(0, 90));

  // and a pipeline must publish it, or CI produces a report nobody sees
  const pipe = await p.evaluate(async () => (await (await fetch("/api/tests/pipeline", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ env: "mock", levels: ["smoke"], drafts: true }) })).json()));
  check("the generated command writes a report", /--html report\.html/.test(pipe.command),
        pipe.command);
  check("and the pipeline publishes it", /report\.html/.test(pipe.yaml));
  check("alongside the JUnit a CI server renders", /results\.xml/.test(pipe.yaml));

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
