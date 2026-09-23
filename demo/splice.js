const { chromium } = require("playwright");
const path=require("path");
let pass=0,fail=0; const errs=[];
const ck=(n,ok,d)=>{ok?pass++:fail++;console.log(`  ${ok?"ok  ":"FAIL"}  ${n}${ok||!d?"":"  — "+d}`)};
(async()=>{
  const b=await chromium.launch();
  const c=await b.newContext({viewport:{width:1500,height:1150},deviceScaleFactor:2,colorScheme:"light"});
  const p=await c.newPage(); p.on("pageerror",e=>errs.push(e.message));
  await p.goto("http://localhost:4100",{waitUntil:"networkidle"}); await p.waitForTimeout(2500);
  await p.locator('nav.side a[data-view="environments"]').click(); await p.waitForTimeout(1200);
  await p.locator("#liveUrl").fill("http://127.0.0.1:4010");
  await p.locator("#btnVerifyLive").click();
  // wait for it to finish
  for (let i=0;i<40;i++){ await p.waitForTimeout(1000);
    if (/finished/.test(await p.locator("#liveElapsed").textContent())) break; }
  const el = await p.locator("#liveElapsed").textContent();
  ck("run finished", /finished/.test(el), el);
  ck("no rows left saying 'waiting for this one'",
     await p.locator("#liveRows .runrow.inflight").count() === 0);
  ck("progress no longer claims anything in flight",
     !/in flight/.test(await p.locator("#liveProgress").textContent()),
     await p.locator("#liveProgress").textContent());
  const rows = await p.locator("#liveRows .runrow").count();
  ck("every operation has a row", rows > 40, String(rows));
  const txt = await p.locator("#liveRows").textContent();
  ck("no spliced row (two markers in one)", !/(PASS|WARN|FAIL)[^]{0,60}\.\.\.\./.test(txt));
  await p.locator("#liveOutCard").screenshot({path:path.join(__dirname,"ui","splice.png")});
  console.log("\n"+"=".repeat(50));
  console.log(`${pass} passed, ${fail} failed`);
  console.log(errs.length?"JS ERRORS:\n  "+errs.join("\n  "):"no JS errors");
  await b.close(); process.exit(fail||errs.length?1:0);
})();
