const { chromium } = require("playwright");
const path=require("path");
let pass=0,fail=0; const errs=[];
const ck=(n,ok,d)=>{ok?pass++:fail++;console.log(`  ${ok?"ok  ":"FAIL"}  ${n}${ok||!d?"":"  — "+d}`)};
(async()=>{
  const b=await chromium.launch();
  const c=await b.newContext({viewport:{width:1500,height:1150},deviceScaleFactor:2,colorScheme:"light"});
  const p=await c.newPage(); p.on("pageerror",e=>errs.push(e.message));
  await p.goto("http://localhost:4100",{waitUntil:"networkidle"}); await p.waitForTimeout(2500);

  // a TARGET step that cannot work without the setup that precedes it
  await p.locator('nav.side a[data-view="explore"]').click(); await p.waitForTimeout(700);
  await p.locator("#method").selectOption("POST");
  await p.locator("#path").fill("/api/v1/recruitment-settings/positions/departments/{{departmentId}}/levels");
  await p.locator("#body").fill(JSON.stringify({title:"Senior",level_order:1},null,2));
  await p.locator("#btnSend").click(); await p.waitForTimeout(1300);
  await p.locator("#btnSaveTest").click(); await p.waitForTimeout(1200);

  await p.locator("#dlgSuite").fill("departments");
  await p.locator("#dlgTarget").selectOption("api"); await p.waitForTimeout(300);
  await p.locator("#dlgScenario").fill("level-needs-a-department");
  await p.locator("#dlgRole").selectOption("target");
  await p.locator("#dlgId").fill("chain-probe");
  await p.locator("#dlgName").fill("create a level under it");

  console.log("\nRUN NOW on a TARGET step of an existing scenario");
  await p.locator("#dlgRun").click(); await p.waitForTimeout(4000);
  const out = await p.locator("#dlgTry").textContent();
  ck("ran more than one step", /create the parent department/.test(out), out.slice(0,120));
  ck("the setup ran first", out.indexOf("create the parent department") < out.indexOf("create a level"));
  ck("overall verdict shown", /pass|fail/.test(out));
  ck("no undefined-variable error", !/is not defined/.test(out), out.slice(0,140));
  await p.locator("#saveDlg").screenshot({path:path.join(__dirname,"ui","chain.png")});

  console.log("\n"+"=".repeat(50));
  console.log(`${pass} passed, ${fail} failed`);
  console.log(errs.length?"JS ERRORS:\n  "+errs.join("\n  "):"no JS errors");
  await b.close(); process.exit(fail||errs.length?1:0);
})();
