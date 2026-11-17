/**
 * probe_dom.js -- runs INSIDE the sandbox container (Sec10). Delivered by
 * play_agent.tools.contracts.probe_live_dom as `node -e <this source> <json>`,
 * so nothing has to be mounted into the container for it to run.
 *
 * Input: the LAST command-line argument is a JSON string
 *   { appUrl, candidateExpression, replayActions }
 * (`node script.js '<json>'` and `node -e '<code>' '<json>'` put it at
 * different argv indexes, but it is last in both).
 *
 * Output: the LAST non-blank stdout line is one JSON object
 *   { resolved: bool, matchedCount: int, isVisible: bool }
 * Anything else may be printed before it; the Python side trusts only that
 * last line. Exit 0 means the probe ran (resolved may still be false -- a
 * valid result, not a failure); non-zero means the probe itself errored.
 *
 * candidateExpression is a Playwright locator expression such as
 *   page.getByRole("button", { name: "Sign in" })
 * It is evaluated against the live page. That is only safe because the
 * Python caller has ALREADY validated it against the locator-method
 * allowlist with literal-only arguments (the same check apply_patch uses)
 * and refuses to send anything else; this script does not re-validate.
 *
 * replayActions are simple recorded steps, each one of
 *   { type: "goto", url }  { type: "click", selector }
 *   { type: "fill", selector, value }
 * Unrecognized types are skipped with a warning on stderr rather than
 * aborting: one unreplayable step should not block checking the candidate.
 *
 * One browser context per call, always closed in `finally`; nothing
 * persists between probes.
 */

function loadChromium() {
  try {
    return require("playwright").chromium;
  } catch (_) {
    // The test image always has @playwright/test; the bare `playwright`
    // package is not guaranteed.
    return require("@playwright/test").chromium;
  }
}

function parseInput() {
  return JSON.parse(process.argv[process.argv.length - 1]);
}

function buildLocator(page, expression) {
  return new Function("page", `"use strict"; return (${expression});`)(page);
}

async function replayAction(page, action) {
  switch (action.type) {
    case "goto":
      await page.goto(action.url);
      return;
    case "click":
      await page.locator(action.selector).click();
      return;
    case "fill":
      await page.locator(action.selector).fill(action.value);
      return;
    default:
      process.stderr.write(`probe_dom: skipping unrecognized action type '${action.type}'\n`);
  }
}

async function main() {
  const { appUrl, candidateExpression, replayActions } = parseInput();

  const browser = await loadChromium().launch();
  let result;

  try {
    const context = await browser.newContext();
    const page = await context.newPage();

    await page.goto(appUrl);
    for (const action of replayActions || []) {
      await replayAction(page, action);
    }

    const locator = buildLocator(page, candidateExpression);
    const matchedCount = await locator.count();
    const resolved = matchedCount > 0;
    // Visibility of the first match only; uniqueness is reported separately
    // by the Python side from matchedCount.
    const isVisible = resolved ? await locator.first().isVisible() : false;

    result = { resolved, matchedCount, isVisible };
  } finally {
    await browser.close();
  }

  console.log(JSON.stringify(result));
}

main().catch((err) => {
  process.stderr.write(`probe_dom: ${err.stack || err}\n`);
  process.exit(1);
});
