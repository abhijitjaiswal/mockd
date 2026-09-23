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
  // One route decides both the operation to untick and the group to narrow to.
  // Deriving them separately let them disagree, so the unticked operation fell
  // outside the filtered run and never appeared as SKIP.
  const chosen = await p.evaluate(async () => {
    const all = (await (await fetch("/api/routes")).json());
    const routes = all.routes || all;
    // keep the leading slash: without it startsWith() never matches a path
    const group = (path) => {
      const parts = path.split("/").filter(Boolean);
      return "/" + parts.slice(0, parts.length - 1).join("/") + "/";
    };
    // the run has to contain BOTH a streaming operation and one we untick, so
    // start from the stream and find a sibling to deselect
    const streamy = routes.find((x) => /stream|sse|watch|subscribe|events/i.test(x.path));
    if (streamy) {
      const g = group(streamy.path).replace(/[^/]+\/$/, "");
      const sibling = routes.find((x) => x.method === "GET" && !/\{/.test(x.path)
        && x.path.startsWith(g) && x.path !== streamy.path);
      // filter by the sibling's FULL path: a bare last segment ("list")
      // matches many operations, and the first one unticked can sit outside
      // the group this run is narrowed to
      if (sibling) return { term: sibling.path, group: g };
    }
    const r = routes.find((x) => x.method === "GET" && !/\{/.test(x.path));
    if (!r) return null;
    return { term: r.path, group: group(r.path), noStream: true };
  });
  const term = chosen ? chosen.term : "";
  await p.locator("#opsFilter").fill(term); await p.waitForTimeout(400);
  await p.locator("#opsPick input").first().uncheck(); await p.waitForTimeout(200);
  await p.locator("#opsFilter").fill(""); await p.waitForTimeout(300);
  ck("unticking reduces the count",
     /^(\d+) of \1$/.test((await p.locator("#opsCount").textContent()).trim()) === false,
     await p.locator("#opsCount").textContent());
  ck("skip-streaming is on by default", await p.locator("#liveSkipStream").isChecked());

  await p.locator("#liveUrl").fill("http://127.0.0.1:4010");
  // the same route's group, so the operation unticked above is inside this run
  await p.locator("#liveOnly").fill(chosen ? chosen.group : "");
  await p.locator("#btnVerifyLive").click(); await p.waitForTimeout(8000);
  const out = await p.locator("#liveRows").textContent();
  ck("deselected operation reported as SKIP", /deselected/.test(out));
  if (!chosen || !chosen.noStream) {
    ck("streaming operation skipped", /looks like a stream/.test(out));
  } else {
    console.log("  --    this spec has no streaming operation to skip");
  }
  await p.locator("#liveOutCard").screenshot({path:path.join(__dirname,"ui","picker.png")});

  console.log("\n"+"=".repeat(50));
  console.log(`${pass} passed, ${fail} failed`);
  console.log(errs.length?"JS ERRORS:\n  "+errs.join("\n  "):"no JS errors");
  await b.close(); process.exit(fail||errs.length?1:0);
})();
