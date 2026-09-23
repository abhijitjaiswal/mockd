const { chromium } = require("playwright");
const path=require("path");
let pass=0,fail=0; const errs=[];
const ck=(n,ok,d)=>{ok?pass++:fail++;console.log(`  ${ok?"ok  ":"FAIL"}  ${n}${ok||!d?"":"  — "+d}`)};
(async()=>{
  const b=await chromium.launch();
  const c=await b.newContext({viewport:{width:1500,height:1000},deviceScaleFactor:2,colorScheme:"light"});
  const p=await c.newPage();
  p.on("pageerror",e=>errs.push(e.message));
  await p.goto("http://localhost:4100",{waitUntil:"networkidle"}); await p.waitForTimeout(2500);

  console.log("\nSAVE DIALOG — assertion editor");
  await p.locator('nav.side a[data-view="explore"]').click(); await p.waitForTimeout(700);
  await p.locator("#method").selectOption("GET");
  await p.locator("#path").fill("/api/v1/recruitment-settings/positions/departments");
  await p.locator("#btnSend").click(); await p.waitForTimeout(1300);
  await p.locator("#btnSaveTest").click(); await p.waitForTimeout(1200);
  const seeded = await p.locator("#dlgAsserts .arow2").count();
  ck("suggestions seeded as editable rows", seeded > 3, String(seeded));

  await p.locator("#dlgAsserts .add-assert").click(); await p.waitForTimeout(300);
  ck("add a row", await p.locator("#dlgAsserts .arow2").count() === seeded+1);
  const row = p.locator("#dlgAsserts .arow2").last();
  await row.locator(".a-type").selectOption("jsonpath");
  await row.locator(".a-path").fill("data.total");
  await row.locator(".a-op").selectOption("gte");
  await row.locator(".a-value").fill("1");
  ck("value box visible for gte", await row.locator(".a-value").isVisible());

  await p.locator("#dlgAsserts .add-assert").click(); await p.waitForTimeout(250);
  const r2 = p.locator("#dlgAsserts .arow2").last();
  await r2.locator(".a-type").selectOption("schema");
  ck("schema row hides path/op/value",
     !(await r2.locator(".a-path").isVisible()) && !(await r2.locator(".a-value").isVisible()));

  await p.locator("#dlgAsserts .add-assert").click(); await p.waitForTimeout(250);
  const r3 = p.locator("#dlgAsserts .arow2").last();
  await r3.locator(".a-type").selectOption("jsonpath");
  await r3.locator(".a-path").fill("data.items");
  await r3.locator(".a-op").selectOption("not_empty");
  ck("value box hidden for not_empty", !(await r3.locator(".a-value").isVisible()));

  await p.locator("#dlgAsserts .arow2").first().locator(".a-del").click(); await p.waitForTimeout(250);
  ck("remove a row", await p.locator("#dlgAsserts .arow2").count() === seeded+2);

  await p.locator("#dlgSuite").fill("departments");
  await p.locator("#dlgId").fill("assert-editor-probe");
  await p.locator("#dlgName").fill("Assertion editor probe");
  await p.locator("#saveDlg").screenshot({path:path.join(__dirname,"ui","assert.png")});
  await p.locator("#dlgSave").click(); await p.waitForTimeout(1500);
  ck("saved", !(await p.locator("#saveDlg").isVisible()));

  console.log("\nEDIT DIALOG — structured editing");
  await p.locator('nav.side a[data-view="tests"]').click(); await p.waitForTimeout(1500);
  const row2 = p.locator(".titem").filter({hasText:"Assertion editor probe"}).first();
  await row2.locator('button[data-act="edit"]').click(); await p.waitForTimeout(1200);
  const n = await p.locator("#editBuilt .arow2").count();
  ck("existing assertions load into the editor", n > 0, String(n));
  await p.locator("#editBuilt .add-assert").first().click(); await p.waitForTimeout(250);
  const last = p.locator("#editBuilt .arow2").last();
  await last.locator(".a-type").selectOption("responseTime");
  await last.locator(".a-op").selectOption("lt");
  await last.locator(".a-value").fill("1500");
  await p.locator("#btnEditRaw").click(); await p.waitForTimeout(400);
  const raw = await p.locator("#editBody").inputValue();
  ck("builder round-trips into JSON", raw.includes("responseTime") && raw.includes("1500"));
  await p.locator("#btnEditRaw").click(); await p.waitForTimeout(400);
  await p.locator("#editSave").click(); await p.waitForTimeout(1200);
  ck("edit saved", !(await p.locator("#editDlg").isVisible()));

  console.log("\n"+"=".repeat(50));
  console.log(`${pass} passed, ${fail} failed`);
  console.log(errs.length?"JS ERRORS:\n  "+errs.join("\n  "):"no JS errors");
  await b.close(); process.exit(fail||errs.length?1:0);
})();
