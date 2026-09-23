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

  console.log("\nOPERATION PICKER");
  const n = await p.locator("#opsPick label").count();
  ck("operations listed with checkboxes", n > 50, String(n));
  ck("count shown", /of \d+ selected/.test(await p.locator("#opsCount").textContent()),
     await p.locator("#opsCount").textContent());
  ck("streaming ones are marked", await p.locator("#opsPick .stream").count() > 0);

  await p.locator("#opsNone").click(); await p.waitForTimeout(300);
  ck("none clears them", (await p.locator("#opsCount").textContent()).startsWith("0 of"));
  await p.locator("#opsReads").click(); await p.waitForTimeout(300);
  const readsTxt = await p.locator("#opsCount").textContent();
  // how many GETs there are depends on which spec is loaded, so count them
  // rather than writing one spec's number into the assertion
  const gets = await p.evaluate(async () => {
    const d = await (await fetch("/api/routes")).json();
    return (d.routes || d).filter((r) => r.method === "GET").length;
  });
  ck("reads only selects GETs", new RegExp(`^${gets} of`).test(readsTxt),
     `${readsTxt} (spec has ${gets} GETs)`);

  await p.locator("#opsFilter").fill("metadata"); await p.waitForTimeout(400);
  const filtered = await p.locator("#opsPick label").count();
  ck("filter narrows the picker", filtered > 0 && filtered < n, String(filtered));
  await p.locator("#opsFilter").fill(""); await p.waitForTimeout(300);

  // untick one and run; it must be reported as deselected
  await p.locator("#opsAll").click(); await p.waitForTimeout(300);
  await p.locator("#opsFilter").fill("user/list"); await p.waitForTimeout(400);
  await p.locator("#opsPick input").first().uncheck(); await p.waitForTimeout(200);
  await p.locator("#opsFilter").fill(""); await p.waitForTimeout(300);
  ck("unticking reduces the count",
     !(await p.locator("#opsCount").textContent()).startsWith("96 of"),
     await p.locator("#opsCount").textContent());
  ck("skip-streaming is on by default", await p.locator("#liveSkipStream").isChecked());

  await p.locator("#liveUrl").fill("http://127.0.0.1:4010");
  await p.locator("#liveOnly").fill("user/");
  await p.locator("#btnVerifyLive").click(); await p.waitForTimeout(8000);
  const out = await p.locator("#liveRows").textContent();
  ck("deselected operation reported as SKIP", /deselected/.test(out));
  ck("streaming operation skipped", /looks like a stream/.test(out));
  await p.locator("#liveOutCard").screenshot({path:path.join(__dirname,"ui","picker.png")});

  console.log("\n"+"=".repeat(50));
  console.log(`${pass} passed, ${fail} failed`);
  console.log(errs.length?"JS ERRORS:\n  "+errs.join("\n  "):"no JS errors");
  await b.close(); process.exit(fail||errs.length?1:0);
})();
