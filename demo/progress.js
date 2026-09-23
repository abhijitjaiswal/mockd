const { chromium } = require("playwright");
const path=require("path");
let pass=0,fail=0; const errs=[];
const ck=(n,ok,d)=>{ok?pass++:fail++;console.log(`  ${ok?"ok  ":"FAIL"}  ${n}${ok||!d?"":"  — "+d}`)};
(async()=>{
  const b=await chromium.launch();
  const c=await b.newContext({viewport:{width:1500,height:1100},deviceScaleFactor:2,colorScheme:"light"});
  const p=await c.newPage(); p.on("pageerror",e=>errs.push(e.message));
  await p.goto("http://localhost:4100",{waitUntil:"networkidle"}); await p.waitForTimeout(2500);

  console.log("\nLIVE PROGRESS — verify against a deliberately slow target");
  await p.locator('nav.side a[data-view="environments"]').click(); await p.waitForTimeout(800);
  await p.locator("#liveUrl").fill("http://127.0.0.1:4010");
  await p.locator("#liveHeaders").fill("X-Mock-Delay: 300");
  await p.locator("#btnVerifyLive").click();

  await p.waitForTimeout(3500);
  const early = await p.locator("#liveRows .runrow").count();
  ck("rows appear while still running", early > 0, String(early));
  ck("cancel button visible mid-run", await p.locator("#btnCancelLive").isVisible());
  const prog1 = await p.locator("#liveProgress").textContent();
  ck("progress counter shows", /\d/.test(prog1), prog1);
  ck("elapsed ticking", /running/.test(await p.locator("#liveElapsed").textContent()));

  await p.waitForTimeout(5000);
  const later = await p.locator("#liveRows .runrow").count();
  ck("row count grows as it runs", later > early, `${early} -> ${later}`);
  // skipped rows legitimately have no status, so check the set, not the first
  ck("rows carry a status code",
     /\d{3}/.test(await p.locator("#liveRows").textContent()));
  ck("phase headings rendered", await p.locator("#liveRows .runphase").count() > 0);
  await p.locator("#liveOutCard").screenshot({path:path.join(__dirname,"ui","progress.png")});

  await p.locator("#liveRaw").click(); await p.waitForTimeout(400);
  ck("raw log toggle works", await p.locator("#liveOut").isVisible());
  await p.locator("#liveRaw").click();

  await p.locator("#btnCancelLive").click(); await p.waitForTimeout(3000);
  ck("cancel stops it", /cancelled|finished/.test(await p.locator("#liveElapsed").textContent()),
     await p.locator("#liveElapsed").textContent());

  console.log("\n"+"=".repeat(50));
  console.log(`${pass} passed, ${fail} failed`);
  console.log(errs.length?"JS ERRORS:\n  "+errs.join("\n  "):"no JS errors");
  await b.close(); process.exit(fail||errs.length?1:0);
})();
