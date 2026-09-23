const { chromium } = require("playwright");
const path=require("path");
let pass=0,fail=0; const errs=[];
const ck=(n,ok,d)=>{ok?pass++:fail++;console.log(`  ${ok?"ok  ":"FAIL"}  ${n}${ok||!d?"":"  — "+d}`)};
(async()=>{
  const b=await chromium.launch();
  const c=await b.newContext({viewport:{width:1500,height:1100},deviceScaleFactor:2,colorScheme:"light"});
  const p=await c.newPage(); p.on("pageerror",e=>errs.push(e.message));
  await p.goto("http://localhost:4100",{waitUntil:"networkidle"}); await p.waitForTimeout(2500);

  await p.locator('nav.side a[data-view="explore"]').click(); await p.waitForTimeout(700);
  await p.locator("#method").selectOption("GET");
  await p.locator("#path").fill("/api/v1/recruitment-settings/positions/departments");
  await p.locator("#btnSend").click(); await p.waitForTimeout(1300);
  await p.locator("#btnSaveTest").click(); await p.waitForTimeout(1200);

  console.log("\nRUN NOW — save dialog");
  ck("button present", await p.locator("#dlgRun").count()===1);
  await p.locator("#dlgRun").click(); await p.waitForTimeout(2500);
  const out = await p.locator("#dlgTry").textContent();
  ck("verdict shown", /pass|fail/.test(out), out.slice(0,60));
  ck("per-assertion lines", await p.locator("#dlgTry .tryline").count() > 3);

  // add a deliberately false assertion and re-run
  await p.locator("#dlgAsserts .add-assert").click(); await p.waitForTimeout(300);
  const r = p.locator("#dlgAsserts .arow2").last();
  await r.locator(".a-type").selectOption("jsonpath");
  await r.locator(".a-path").fill("data.definitely_not_here");
  await r.locator(".a-op").selectOption("exists");
  await p.locator("#dlgRun").click(); await p.waitForTimeout(2500);
  ck("a false assertion is reported failing",
     await p.locator("#dlgTry .tryline.no").count() >= 1);
  ck("the passing ones still show ok",
     await p.locator("#dlgTry .tryline.ok").count() >= 3);
  await p.locator("#saveDlg").screenshot({path:path.join(__dirname,"ui","runnow.png")});

  // fix it and confirm green
  await r.locator(".a-path").fill("data.total");
  await r.locator(".a-op").selectOption("gte");
  await r.locator(".a-value").fill("1");
  await p.locator("#dlgRun").click(); await p.waitForTimeout(2500);
  ck("all green after the fix", (await p.locator("#dlgTry").textContent()).includes("pass")
     && await p.locator("#dlgTry .tryline.no").count()===0);

  await p.locator("#dlgSuite").fill("departments");
  await p.locator("#dlgId").fill("runnow-probe");
  await p.locator("#dlgSave").click(); await p.waitForTimeout(1500);

  console.log("\nRUN NOW — edit dialog");
  await p.locator('nav.side a[data-view="tests"]').click(); await p.waitForTimeout(1500);
  const row = p.locator(".titem").filter({hasText:"runnow-probe"}).first();
  const anyRow = await row.count() ? row : p.locator(".titem").first();
  await anyRow.locator('button[data-act="edit"]').click(); await p.waitForTimeout(1200);
  ck("button present in edit", await p.locator("#editRun").count()===1);
  await p.locator("#editRun").click(); await p.waitForTimeout(3000);
  ck("edit dialog shows a verdict",
     /pass|fail/.test(await p.locator("#editTry").textContent()));
  await p.locator("#editCancel").click();

  console.log("\n"+"=".repeat(50));
  console.log(`${pass} passed, ${fail} failed`);
  console.log(errs.length?"JS ERRORS:\n  "+errs.join("\n  "):"no JS errors");
  await b.close(); process.exit(fail||errs.length?1:0);
})();
