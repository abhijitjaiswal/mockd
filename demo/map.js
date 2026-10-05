/**
 * map.js — the API map on Home.
 *
 * It is drawn to be watched, so the suite checks the part that must not be
 * decoration: every documented endpoint is on it, what it shows about one is
 * what is true of it, a click leads to that endpoint's tests (or to making
 * one), and running from it ends with what the run actually found.
 *
 * It only ever runs against the mock.
 */
const { chromium } = require("playwright");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + String(d).slice(0, 300)}`); };

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1360, height: 900 } });
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  const flat = (s) => (s || "").replace(/\s+/g, " ").trim();
  const toast = async () => flat(await p.locator("#toast").textContent().catch(() => ""));
  const data = async () => p.evaluate(async () => (await fetch("/api/map?env=mock")).json());

  await p.waitForFunction(() => document.querySelectorAll("#mapSvg .dot").length > 0, null, { timeout: 20000 }).catch(() => {});
  const d = await data();
  check("Home shows a map of the API", await p.locator("#mapCard").isVisible());
  check("with a dot for every documented endpoint", await p.locator("#mapSvg .dot").count() === d.total,
        `${await p.locator("#mapSvg .dot").count()} of ${d.total}`);
  check("grouped by resource, each named",
        await p.locator("#mapSvg .cluster").count() === d.resources.length
        && await p.locator("#mapSvg .label").count() === d.resources.length);
  check("the sentence above it is the count, in words", flat(await p.locator("#mapSentence").textContent()) === d.sentence,
        flat(await p.locator("#mapSentence").textContent()));
  await p.waitForFunction((h) => document.querySelector("#mapPct").textContent === h + "%", d.health, { timeout: 8000 }).catch(() => {});
  check("and the number in the middle is the share of endpoints proven",
        (await p.locator("#mapPct").textContent()) === d.health + "%", await p.locator("#mapPct").textContent());
  for (const state of ["pass", "fail", "idle", "none"]) {
    const drawn = await p.locator(`#mapSvg .dot.${state}`).count();
    check(`dots drawn as "${state}" match what is known (${d.counts[state]})`, drawn === d.counts[state], String(drawn));
  }
  const overlap = await p.evaluate(() => {
    const boxes = [...document.querySelectorAll("#mapSvg .label")].map((el) => el.getBoundingClientRect());
    return boxes.some((a, i) => boxes.some((b, j) => i < j && a.left < b.right - 1 && b.left < a.right - 1
      && a.top < b.bottom - 1 && b.top < a.bottom - 1));
  });
  check("no two resource names run into each other", !overlap);

  // ------------------------------------------------------ hover: what it is
  const all = d.resources.flatMap((r) => r.endpoints);
  const tested = all.find((e) => e.tests.length);
  const dot = (key) => p.locator(`#mapSvg .dot[data-mapkey="${key.replace(/"/g, '\\"')}"]`);
  await dot(tested.key).hover();
  await p.waitForTimeout(250);
  const tip = flat(await p.locator("#mapTip").textContent());
  check("hovering an endpoint says which it is and what is known", tip.includes(tested.key)
        && new RegExp(`${tested.tests.length} tests?`).test(tip), tip);
  await p.mouse.move(5, 5);
  await p.waitForTimeout(200);
  check("and the note goes away when the pointer does", await p.locator("#mapTip").isHidden());

  // ------------------------------------------------- click: to its tests
  await dot(tested.key).click();
  await p.waitForTimeout(1500);
  check("clicking an endpoint opens Tests", await p.locator('.view[data-view="tests"]').isVisible());
  check("showing only the tests that call it", await p.locator("#libOnly").isVisible()
        && flat(await p.locator("#libOnly").textContent()).includes(tested.key), flat(await p.locator("#libOnly").textContent()));
  check("exactly as many as the map said", await p.evaluate(() => /^(\d+) of \d+$/.exec(document.querySelector("#libCount").textContent.trim())?.[1])
        === String(tested.tests.length) || (await p.locator("#libList .librow").count()) === Math.min(25, tested.tests.length),
        await p.locator("#libCount").textContent());
  await p.locator("#libOnlyOff").click();

  const untested = all.find((e) => !e.tests.length);
  if (untested) {
    await p.locator('nav.side a[data-view="home"]').click();
    await p.waitForFunction(() => document.querySelectorAll("#mapSvg .dot").length > 0, null, { timeout: 15000 }).catch(() => {});
    await p.waitForTimeout(600);
    await dot(untested.key).click();
    await p.waitForTimeout(900);
    check("clicking an endpoint nothing tests opens Create tests instead",
          await p.locator('.view[data-view="create"]').isVisible());
    check("with the sentence started for you", (await p.locator("#czStory").inputValue()).includes(untested.key),
          await p.locator("#czStory").inputValue());
    await p.locator("#czStory").fill("");
  }

  // ---------------------------------------------- run from it, and watch
  await p.locator('nav.side a[data-view="home"]').click();
  await p.waitForFunction(() => document.querySelectorAll("#mapSvg .dot").length > 0, null, { timeout: 15000 }).catch(() => {});
  check("one button runs every test on the server shown", /Run every test on mock/.test(await p.locator("#mapRun").textContent()));
  await p.locator("#mapRun").click();
  await p.waitForFunction(() => /Running on mock/.test(document.querySelector("#mapRun").textContent), null, { timeout: 15000 }).catch(() => {});
  check("while it runs the button says so", /Running on mock… \d+s/.test(await p.locator("#mapRun").textContent()),
        await p.locator("#mapRun").textContent());
  const seen = await p.evaluate(() => new Promise((resolve) => {
    let most = 0; const until = Date.now() + 2500;
    const look = () => { most = Math.max(most, document.querySelectorAll("#mapSvg .packet").length);
                         Date.now() < until ? requestAnimationFrame(look) : resolve(most); };
    look();
  }));
  check("and calls can be seen going out", seen > 0, String(seen));
  await p.waitForFunction(() => /Run every test on mock/.test(document.querySelector("#mapRun").textContent),
                          null, { timeout: 300000 }).catch(() => {});
  const after = await data();
  check("when it ends, the sentence is what the run found",
        flat(await p.locator("#mapSentence").textContent()) === after.sentence, flat(await p.locator("#mapSentence").textContent()));
  check("it is said where you pressed, too", (await toast()).includes(after.sentence), await toast());
  check("every dot has settled to what was found",
        await p.locator("#mapSvg .dot.pass").count() === after.counts.pass
        && await p.locator("#mapSvg .dot.fail").count() === after.counts.fail,
        `${await p.locator("#mapSvg .dot.pass").count()}/${after.counts.pass} pass, ${await p.locator("#mapSvg .dot.fail").count()}/${after.counts.fail} fail`);
  check("nothing is left in flight", await p.locator("#mapSvg .packet").count() === 0);

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
