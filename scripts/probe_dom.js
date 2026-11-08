#!/usr/bin/env node
/**
 * probe_dom.js -- runs INSIDE the sandbox container (Sec10), invoked by
 * play_agent.tools.contracts.probe_live_dom via Sandbox.exec_run.
 *
 * Contract with the Python caller:
 *   argv[2] is a single JSON string: { appUrl, candidateSelector, replayActions }
 *   stdout's LAST non-blank line is a single JSON object:
 *     { resolved: bool, matchedCount: int, isVisible: bool }
 *   Any other stdout (browser console passthrough, progress) must come
 *   BEFORE that line, never after -- the Python side only trusts the
 *   last line, by design, so this script can log freely for debugging
 *   without breaking the contract.
 *   Exit code 0 means the probe ran to completion (resolved may still be
 *   false -- that's a valid probe result, not a script failure). Non-zero
 *   means the probe itself errored (bad selector syntax, navigation
 *   failure, etc.) -- Python surfaces this as ToolError.
 *
 * replayActions is a list of recorded steps, each shaped as one of:
 *   { "type": "goto", "url": "/cart" }
 *   { "type": "click", "selector": "..." }
 *   { "type": "fill", "selector": "...", "value": "..." }
 *   Unrecognized action types are skipped with a warning rather than
 *   aborting the whole probe -- a single unreplayable step shouldn't
 *   block checking the candidate selector, which is the actual point
 *   of this call.
 *
 * Resource limits (Sec7): single browser context per probe, always torn
 * down in `finally`, no persisted storage across probes -- the context
 * exists only for the lifetime of this process.
 */

const { chromium } = require("playwright");

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
  const input = JSON.parse(process.argv[2]);
  const { appUrl, candidateSelector, replayActions } = input;

  const browser = await chromium.launch();
  let result;

  try {
    const context = await browser.newContext();
    const page = await context.newPage();

    await page.goto(appUrl);

    for (const action of replayActions || []) {
      await replayAction(page, action);
    }

    const locator = page.locator(candidateSelector);
    const matchedCount = await locator.count();
    const resolved = matchedCount > 0;
    // isVisible on a multi-match locator checks the first match only --
    // uniqueness itself is reported separately by the Python side via
    // matchedCount, so this doesn't try to average/aggregate visibility
    // across matches.
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
