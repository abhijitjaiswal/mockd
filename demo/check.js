const { chromium } = require("playwright");
const path = require("path");
(async () => {
  const b = await chromium.launch();
  const c = await b.newContext({ viewport:{width:1500,height:1000}, deviceScaleFactor:2,
                                 colorScheme:"light" });
  const p = await c.newPage();
  const errs = [];
  p.on("pageerror", e => errs.push("pageerror: " + e.message));
  p.on("console", m => { if (m.type()==="error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil:"networkidle" });
  await p.waitForTimeout(2500);
  for (const v of ["overview","authoring","source","connect","explore","tests","environments","server"]) {
    await p.locator(`nav.side a[data-view="${v}"]`).click();
    await p.waitForTimeout(900);
    const on = await p.locator(`.view[data-view="${v}"].on`).count();
    const h = await p.locator(`.view[data-view="${v}"]`).evaluate(e => e.scrollHeight);
    console.log(`  ${v.padEnd(13)} visible=${on===1}  height=${h}px`);
    await p.screenshot({ path: path.join(__dirname,"ui",`${v}.png`), fullPage:false });
  }
  console.log(errs.length ? "\nJS ERRORS:\n  " + errs.join("\n  ") : "\nno JS errors");
  await b.close();
})();
