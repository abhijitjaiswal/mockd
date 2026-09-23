const { chromium } = require("playwright");
const { guard } = require("./dotenv");

// this suite writes real values through the console's own dialog — snapshot
// .env so the run leaves the machine as it found it
guard();
const path=require("path");
let pass=0,fail=0; const errs=[];
const ck=(n,ok,d)=>{ok?pass++:fail++;console.log(`  ${ok?"ok  ":"FAIL"}  ${n}${ok||!d?"":"  — "+d}`)};
(async()=>{
  const b=await chromium.launch();
  const c=await b.newContext({viewport:{width:1500,height:1150},deviceScaleFactor:2,colorScheme:"light"});
  const p=await c.newPage(); p.on("pageerror",e=>errs.push(e.message));
  await p.goto("http://localhost:4100",{waitUntil:"networkidle"}); await p.waitForTimeout(2500);
  await p.locator('nav.side a[data-view="environments"]').click(); await p.waitForTimeout(1200);

  console.log("\nSET VARS FROM THE UI");
  ck("every environment has a Configure button",
     await p.locator(".env-config").count() >= 5);
  const devRow = p.locator("#envList tr").filter({hasText:"dev"}).first();
  ck("dev shows what it needs", /needs/.test(await devRow.textContent()));

  await p.locator('.env-config[data-env="staging"]').click(); await p.waitForTimeout(900);
  ck("dialog lists the variables", await p.locator("#envDlgFields .varrow").count() === 3);
  ck("secrets use a password field",
     await p.locator('#envDlgFields input[type="password"]').count() >= 1);
  ck("purpose explained per variable",
     /root of the API/.test(await p.locator("#envDlgFields").textContent()));
  ck("says where it writes",
     /\.env/.test(await p.locator("#envDlgWhere").textContent()));
  await p.locator("#envDlg").screenshot({path:path.join(__dirname,"ui","envvars.png")});

  await p.locator('input[data-var="STAGING_BASE_URL"]').fill("http://127.0.0.1:4010");
  await p.locator('input[data-var="STAGING_TOKEN"]').fill("ui-typed-token");
  await p.locator('input[data-var="STAGING_DEPARTMENT_ID"]').fill("dept-42");
  await p.locator("#envDlgSave").click(); await p.waitForTimeout(1800);
  ck("save confirmed", /written to/.test(await p.locator("#envDlgMsg").textContent()),
     await p.locator("#envDlgMsg").textContent());
  await p.waitForTimeout(1200);

  const rowNow = await p.locator("#envList tr").filter({hasText:"staging"}).first().textContent();
  ck("environment now reports ready", /ready/.test(rowNow), rowNow.slice(0,90));

  // and it is usable immediately
  await p.locator("#liveEnv").selectOption("staging"); await p.waitForTimeout(500);
  ck("selectable for a live run",
     !/needs/.test(await p.locator("#envHint").textContent()),
     await p.locator("#envHint").textContent());

  console.log("\n"+"=".repeat(50));
  console.log(`${pass} passed, ${fail} failed`);
  console.log(errs.length?"JS ERRORS:\n  "+errs.join("\n  "):"no JS errors");
  await b.close(); process.exit(fail||errs.length?1:0);
})();
