const { chromium } = require("playwright");
const EP = require("./endpoints");
const { guard } = require("./dotenv");

/* This suite needs one environment that actually resolves, so it provisions a
   throwaway .env and puts the file back exactly as it found it. The token is
   obviously fake — the point is the shape of the command, not the credential. */
guard([
  "DEV_BASE_URL=https://api.example.com",
  "DEV_COOKIE=access_token=probe-not-a-real-token",
  "DEV_DEPARTMENT_ID=d1",
].join("\n") + "\n");
let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };
(async () => {
  const b = await chromium.launch();
  const c = await b.newContext({ permissions: ["clipboard-read", "clipboard-write"] });
  const p = await c.newPage();
  p.on("pageerror", e => errs.push("pageerror: " + e.message));
  p.on("console", m => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(2000);
  await p.locator('nav.side a[data-view="explore"]').click(); await p.waitForTimeout(1200);

  check("hidden while the target is the mock", await p.locator("#btnCurlReal").isHidden());

  const opts = await p.locator("#exploreTarget option").allTextContents();
  const dev = opts.find(o => o.includes("dev-cookie"));
  check("dev-cookie is selectable", !!dev, opts.join(" | "));
  await p.locator("#exploreTarget").selectOption({ label: dev }); await p.waitForTimeout(800);
  check("appears once a real environment is chosen", await p.locator("#btnCurlReal").isVisible());

  await p.locator("#method").selectOption("GET");
  await p.locator("#path").fill(EP.GROUPS);

  await p.locator("#btnCurl").click(); await p.waitForTimeout(900);
  const safe = await p.evaluate(() => navigator.clipboard.readText());
  check("plain copy carries the placeholder", safe.includes("<use_your_token>"), safe.slice(-70));
  check("plain copy keeps the cookie name", safe.includes("access_token="));

  await p.locator("#btnCurlReal").click(); await p.waitForTimeout(900);
  const real = await p.evaluate(() => navigator.clipboard.readText());
  check("reveal copy has no placeholder", !real.includes("<use_your"), real.slice(-70));
  check("reveal warns in the banner",
        /real credential/i.test(await p.locator("#toast").textContent()));

  await p.locator("#exploreTarget").selectOption("mock"); await p.waitForTimeout(700);
  check("hides again on returning to the mock", await p.locator("#btnCurlReal").isHidden());

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
