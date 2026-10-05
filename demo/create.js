/**
 * create.js — from a sentence to tests that have been tried, by clicking.
 *
 * This is the screen a new QA uses, so it is driven the way they would drive
 * it: type, press, paste, read. The assistant is stood in for by pasting an
 * answer, which is also the path anyone without a local assistant takes.
 *
 * What must hold: nothing off-topic is sent anywhere, a bad paste is explained
 * rather than swallowed, and what comes back is a plain list saying how each
 * test did — not JSON, and not a wall of machinery.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
const EP = require("./endpoints");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const FILE = path.join(DIR, "tests", "drafts", "create-probe.json");
process.on("exit", () => { try { fs.rmSync(FILE, { force: true }); } catch { /* gone */ } });

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  const toast = async () => ((await p.locator("#toast").textContent().catch(() => "")) || "").trim();

  await p.locator('nav.side a[data-view="create"]').click();
  await p.waitForTimeout(1200);

  // ------------------------------------------------------------ describe
  check("the screen opens on one question", await p.locator("#czStory").isVisible());
  check("and nothing else is asked yet",
        await p.locator("#czPaste").isHidden() && await p.locator("#czDone").isHidden());
  check("it says the basics are already covered",
        /baseline/i.test(await p.locator("#czBaseline").textContent()));
  const writers = await p.locator('input[name="czVia"]').count();
  check("it offers who should write the tests", writers >= 1, String(writers));
  check("writing them yourself with any AI tool is always offered",
        await p.locator('input[name="czVia"][value=""]').count() === 1);

  // an empty ask goes nowhere
  await p.locator("#czGo").click();
  await p.waitForTimeout(300);
  check("an empty request is turned back with a plain sentence",
        /sentence/i.test(await toast()), await toast());

  // something that is not about this API is refused before anything is sent
  await p.locator('input[name="czVia"][value=""]').check();
  await p.locator("#czStory").fill("I want to order a pizza with extra cheese tonight");
  await p.locator("#czGo").click();
  await p.waitForTimeout(2500);
  check("a request that is not about this API is refused",
        await p.locator("#czAsk").isVisible() && await p.locator("#czPaste").isHidden(),
        await toast());
  check("and it says why, in words", (await toast()).length > 20, await toast());

  // a real one — an example the screen itself suggests, or the collection's name
  const examples = await p.locator("#czExamples [data-eg]").count();
  if (examples) {
    await p.locator("#czExamples [data-eg]").first().click();
    check("clicking a suggestion fills the box",
          (await p.locator("#czStory").inputValue()).length > 10);
  } else {
    const thing = EP.COLLECTION.split("/").filter(Boolean).pop();
    await p.locator("#czStory").fill(`I want to list ${thing} and check the list answers`);
  }
  await p.locator("summary", { hasText: "Options" }).click();
  await p.locator("#czModule").fill("create-probe");
  await p.locator("#czGo").click();
  await p.waitForFunction(() => !document.querySelector("#czPaste").hidden,
                          null, { timeout: 60000 }).catch(() => {});

  // --------------------------------------------------------------- write
  check("choosing your own tool leads to a prompt to copy",
        await p.locator("#czPaste").isVisible(), await toast());
  const prompt = await p.locator("#czPrompt").textContent();
  check("the prompt is written from the API document",
        prompt.length > 800 && /OPERATIONS THAT LOOK RELEVANT|THE STORY/.test(prompt),
        String(prompt.length));
  check("the step marker has moved on",
        (await p.locator("#czSteps .on").textContent()).includes("Write"));

  // a paste that is not an answer
  await p.locator("#czReply").fill("Sure! Here are some ideas for tests you could write.");
  await p.locator("#czCheck").click();
  await p.waitForTimeout(1500);
  check("a paste with no tests in it is explained, not swallowed",
        /JSON|whole reply/i.test(await toast()) && await p.locator("#czPaste").isVisible(),
        await toast());

  // an answer, wrapped the way assistants wrap them
  const stamp = Date.now().toString(36);
  const answer = "Here you go:\n```json\n" + JSON.stringify([
    { id: `create-probe-ok-${stamp}`, name: "The list answers",
      description: "Asking for the list gives a successful reply.",
      priority: "P1", levels: ["smoke"],
      request: { method: "GET", path: EP.COLLECTION },
      assertions: [{ type: "status", equals: 200 }] },
    { id: `create-probe-bad-${stamp}`, name: "The list is a teapot",
      description: "Deliberately wrong, to see how a failure reads.",
      priority: "P3", levels: ["negative"],
      request: { method: "GET", path: EP.COLLECTION },
      assertions: [{ type: "status", equals: 418 }] },
  ], null, 1) + "\n```\nLet me know if you want more.";
  await p.locator("#czReply").fill(answer);
  await p.locator("#czCheck").click();
  await p.waitForFunction(() => !document.querySelector("#czDone").hidden,
                          null, { timeout: 60000 }).catch(() => {});

  // -------------------------------------------------------------- review
  check("the answer is accepted even with prose and code fences around it",
        await p.locator("#czDone").isVisible(), await toast());
  const head = await p.locator("#czDoneHead").textContent();
  check("the headline says how many were made and how they did",
        /2 tests created/.test(head) && /1 pass/.test(head), head);
  check("each test is a readable row, not JSON",
        await p.locator("#czList .cztest").count() === 2
        && !/[{}]/.test(await p.locator("#czList").textContent()));
  const good = p.locator(`.cztest[data-cz="create-probe-ok-${stamp}"]`);
  const bad = p.locator(`.cztest[data-cz="create-probe-bad-${stamp}"]`);
  check("the one that works says so plainly",
        /Works on the mock/.test(await good.locator(".say").textContent()),
        await good.locator(".say").textContent());
  check("the one that does not says what went wrong",
        /418|status/.test(await bad.locator(".say").textContent()),
        await bad.locator(".say").textContent());
  check("priority and description came through",
        (await good.locator(".prio").textContent()).trim() === "P1"
        && /successful reply/.test(await good.textContent()));
  check("they were saved where they were asked to be", fs.existsSync(FILE));

  // they are in the ordinary list too
  await p.locator("#czOpen").click();
  await p.waitForTimeout(1500);
  check("they appear in the Tests list",
        await p.locator(`[data-id="create-probe-ok-${stamp}"]`).count() >= 1);

  // and can be thrown away again
  await p.locator('nav.side a[data-view="create"]').click();
  await p.waitForTimeout(600);
  if (await p.locator("#czDone").isVisible()) {
    await p.locator("#czDiscard").click();
    await p.waitForTimeout(1200);
    check("removing them takes you back to the question",
          await p.locator("#czAsk").isVisible());
    check("and they are gone from disk", !fs.existsSync(FILE));
  } else {
    check("the review survives a trip to another screen", false, "review was lost");
  }

  // ------------------------------------- an assistant on this machine
  // Letting one write a whole suite takes minutes, so this only checks the part
  // a person sees first: it starts, it says it is working, and Cancel works.
  const local = p.locator('input[name="czVia"]:not([value=""])').first();
  if (await local.count()) {
    await local.check();
    const thing = EP.COLLECTION.split("/").filter(Boolean).pop();
    await p.locator("#czStory").fill(`I want to list ${thing} and check the list answers`);
    await p.locator("#czGo").click();
    await p.waitForFunction(() => !document.querySelector("#czWait").hidden,
                            null, { timeout: 60000 }).catch(() => {});
    check("asking a local assistant shows that it is working",
          await p.locator("#czWait").isVisible(), await toast());
    await p.waitForTimeout(2500);
    check("and how long it has been, with what to expect",
          /\d+s so far/.test(await p.locator("#czWaitNote").textContent()),
          await p.locator("#czWaitNote").textContent());
    await p.locator("#czCancel").click();
    await p.waitForTimeout(1500);
    check("Cancel stops it and returns to the question",
          await p.locator("#czAsk").isVisible() && await p.locator("#czWait").isHidden());
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
