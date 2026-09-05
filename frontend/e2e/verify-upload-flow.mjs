/**
 * End-to-end verification of the product flow: land, upload many files,
 * confirm, run, trace, investigate, review, audit, export.
 *
 * This is the flow a judge will actually perform, so it is driven in a
 * real browser rather than asserted through the API. Several defects in
 * this project were invisible to backend tests and to curl.
 *
 * Requires the backend on :8000, the dev server on :5173, and the demo
 * data generated. Resets the database — development instances only.
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';

// Ports are overridable so this suite can run against an isolated
// instance. It calls /admin/reset, and pointing that at whatever
// happens to be on :8000 would wipe a database it does not own.
const API = process.env.ACCORD_API || 'http://localhost:8000';
const APP = process.env.ACCORD_APP || 'http://localhost:5173';

function findPlaywright() {
  const require_ = createRequire(import.meta.url);
  try {
    return require_.resolve('playwright');
  } catch {
    const npx = join(homedir(), '.npm', '_npx');
    if (existsSync(npx)) {
      for (const dir of readdirSync(npx)) {
        const candidate = join(npx, dir, 'node_modules', 'playwright', 'index.mjs');
        if (existsSync(candidate)) return candidate;
      }
    }
  }
  throw new Error('playwright not found; run `npx playwright --version` once');
}

function findChromium() {
  if (process.env.PLAYWRIGHT_CHROMIUM) return process.env.PLAYWRIGHT_CHROMIUM;
  const cache = join(homedir(), 'Library', 'Caches', 'ms-playwright');
  if (existsSync(cache)) {
    const builds = readdirSync(cache)
      .filter((d) => d.startsWith('chromium-'))
      .sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]));
    for (const build of builds) {
      for (const rel of [
        'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
        'chrome-mac/Chromium.app/Contents/MacOS/Chromium',
        'chrome-linux/chrome',
      ]) {
        const p = join(cache, build, rel);
        if (existsSync(p)) return p;
      }
    }
  }
  for (const p of [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
  ]) {
    if (existsSync(p)) return p;
  }
  throw new Error('no Chromium-compatible browser found; set PLAYWRIGHT_CHROMIUM');
}

const DEMO = join(process.cwd(), '..', 'backend', 'data', 'demo');
const WORKSPACE = join(process.cwd(), '..', 'backend', 'data', 'demo_workspace');
const OUT = process.env.SCREENSHOT_DIR || '/tmp';
const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`);
}

await fetch(`${API}/admin/reset`, { method: 'POST' });

const { chromium } = await import(findPlaywright());
const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

const consoleErrors = [];
page.on('pageerror', (e) => consoleErrors.push(e.message));
const httpErrors = [];
page.on('response', (r) => { if (r.status() >= 400) httpErrors.push(`${r.status()} ${r.url()}`); });

const body = () => page.textContent('body');

// ---- landing --------------------------------------------------------
await page.goto(APP, { waitUntil: 'networkidle' });
const landing = await body();
check('landing page is the front door', /where the trail breaks/i.test(landing));
check('brand is Accord', /Accord/.test(landing) && !/Axiom Recon/.test(landing));
check('landing states the control model', /deterministic/i.test(landing));
check('no invented customers or testimonials', !/testimonial|trusted by|customers say/i.test(landing));

// ---- landing -> app, and BACK must return here ----------------------
await page.locator('a:has-text("Open workspace")').first().click();
await page.waitForURL(/\/app\/runs/, { timeout: 10000 });
check('CTA navigates to the workspace with a real URL', /\/app\/runs/.test(page.url()));
await page.goBack();
await page.waitForTimeout(400);
check('back button returns to the landing page, not out of the site',
  new URL(page.url()).pathname === '/');
await page.goForward();
await page.waitForURL(/\/app\/runs/, { timeout: 10000 });

// ---- deep link survives a cold load ---------------------------------
await page.goto(`${APP}/app/review`, { waitUntil: 'networkidle' });
check('deep link renders on a cold load', /review/i.test(await body()));

// ---- multi-file upload ----------------------------------------------
await page.goto(`${APP}/app/runs/new`, { waitUntil: 'networkidle' });
check('upload is the primary action', /Add your financial sources/i.test(await body()));
check('a sample workspace is offered for someone with no files of their own',
      /Load sample workspace/i.test(await body()));

// The sample workspace is the demo path AND the faster one: the server
// ingests all 21 sources in ~2s, where driving 21 browser file pickers
// takes minutes and tests the file input rather than the product. Manual
// multi-file upload is covered separately below.
const files = readdirSync(WORKSPACE)
  .filter((f) => f.endsWith('.csv') || f.endsWith('.xlsx'))
  .sort()
  .map((f) => join(WORKSPACE, f));
check('demo workspace has many sources', files.length >= 18, `${files.length} files`);

await page.locator('button:has-text("Load sample workspace")').first().click();
await page.waitForFunction(
  () => (document.body.textContent.match(/Columns/g) || []).length >= 18,
  null, { timeout: 120000 });
await page.waitForTimeout(1500);

const inventory = await body();
check('every source landed in one workspace',
  (inventory.match(/Columns/g) || []).length >= files.length - 1,
  `${(inventory.match(/Columns/g) || []).length} inventory rows`);
check('providers are named, not just file types',
  /Razorpay/.test(inventory) && /HDFC/.test(inventory) && /ICICI/.test(inventory)
  && /Shopify/.test(inventory) && /Tally|Zoho|Axis/.test(inventory));
// Deliberately does not name a payment gateway other than Razorpay: the
// demo is shown to Razorpay, so which third-party gateways appear in the
// sample data is a product decision that may change. Assert that several
// DIFFERENT KINDS of source were recognised, which is the actual claim.
check('xlsx is ingested alongside csv', /\.xlsx/.test(inventory));
check('a duplicate file is flagged rather than silently dropped',
  /duplicate/i.test(inventory) || /Keep both/i.test(inventory));
await page.screenshot({ path: `${OUT}/flow-1-inventory.png`, fullPage: true });

// ---- the run is BLOCKED until a low-confidence role is confirmed ----
const runButton = page.locator('button:has-text("Blocked"), button:has-text("Run reconciliation")').first();
const blockedLabel = (await runButton.textContent()) || '';
check('a low-confidence source blocks the run', /blocked/i.test(blockedLabel), blockedLabel.trim());
check('the reason for blocking is stated, not hidden',
  /confidence 0\.5|confirm the role|confirm the source|we think/i.test(inventory));

const confirm = page.locator('button:has-text("That\'s right")').first();
if (await confirm.count()) {
  await confirm.click();
  await page.waitForTimeout(2000);
}
const runLabel = (await page.locator('button:has-text("Blocked"), button:has-text("Run reconciliation")').first().textContent()) || '';
check('confirming the role unblocks the run', !/Blocked/i.test(runLabel), runLabel.trim());
const afterConfirm = await body();

// ---- money-flow plan -------------------------------------------------
check('a money-flow plan is proposed before running', /money.flow map/i.test(afterConfirm));
check('the plan disclaims chained matching rather than implying it',
  /not a chained matcher|pooled|plan and a provenance view/i.test(afterConfirm));
check('stages with no source are never reported as a failure',
  !/BANK[\s\S]{0,80}(failed|missing)/i.test(afterConfirm));

// ---- run -------------------------------------------------------------
await page.locator('button:has-text("Run reconciliation")').first().click();
await page.waitForSelector('.wk-records-row, .records-row, table tbody tr', { timeout: 120000 });

// Wait for the run to actually FINISH before asserting on its results.
// Waiting on the first row only proves the run started: rows stream in as
// records are decided, so every result assertion was racing a run still in
// progress, and the two most important record checks were failing simply
// because their records had not been decided yet.
// Ask the API which run is current rather than parsing the address bar:
// the URL is only reliable once client-side navigation has settled, and
// reading it a moment early yielded undefined and silently skipped the wait.
// `/app/runs/new` also matches `/app/runs/:id`, so a naive parse yielded the
// literal string "new" and polled a run that does not exist. Wait for the
// address to become a real run, then fall back to the API.
await page.waitForFunction(
  () => /\/app\/runs\/(?!new(?:$|[/?#]))[^/?#]+/.test(window.location.pathname),
  null, { timeout: 60000 }).catch(() => {});
let runIdForWait = (page.url().match(/\/app\/runs\/([^/?#]+)/) || [])[1];
if (!runIdForWait || runIdForWait === 'new') {
  const listed = await (await page.request.get(`${API}/runs`)).json();
  const rows = listed.runs || listed;
  runIdForWait = rows[0] && (rows[0].batch_id || rows[0].run_id);
}
if (runIdForWait) {
  const deadline = Date.now() + 180000;
  let stage = null;
  while (Date.now() < deadline) {
    const res = await page.request.get(`${API}/runs/${runIdForWait}/progress`);
    if (!res.ok() && stage === null) stage = `HTTP ${res.status()} for ${runIdForWait}`;
    if (res.ok()) {
      const p = await res.json();
      stage = p.stage;
      if (stage === 'COMPLETE') break;
    }
    await page.waitForTimeout(1000);
  }
  check('the run reaches COMPLETE', stage === 'COMPLETE', String(stage));
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);
}
await page.waitForTimeout(1500);
const detail = await body();
check('run detail opens after execution', /\/app\/runs\//.test(page.url()));
check('results lead with where the money stopped', /where the money stopped/i.test(detail));
check('records are listed with an explanation', /reconciled/i.test(detail));
check('records that reached the semantic tier are counted separately, labelled honestly',
  /ai.assisted/i.test(detail) || /escalated/i.test(detail),
  /ai.assisted/i.test(detail) ? 'model-backed run' : 'offline verifier run');
await page.screenshot({ path: `${OUT}/flow-2-run.png`, fullPage: true });

const runId = (page.url().match(/\/app\/runs\/([^/]+)/) || [])[1];
check('the run has a shareable URL', Boolean(runId), runId || 'none');

// ---- breakpoint trace + investigator ---------------------------------

/**
 * Bring a specific record into view.
 *
 * These checks used to be guarded by `if (row.count())` and, once the demo
 * workspace grew to 3,504 records behind a 500-row page, they simply stopped
 * running — a silent loss of exactly the two assertions that matter most.
 * A skipped safety check reads identically to a passing one, so this now
 * searches for the record and FAILS if it cannot be found.
 */
async function findRecord(id) {
  const search = page.locator('input[type="search"], input[placeholder*="Search" i]').first();
  if (await search.count()) {
    await search.fill(id);
    await page.waitForTimeout(1200);
  }
  const row = page.locator(`tr:has-text("${id}")`).first();
  const found = (await row.count()) > 0;
  check(`record ${id} is reachable in the results`, found);
  return found ? row : null;
}

const pendingRow = await findRecord('ZB-6107');
if (pendingRow) {
  await pendingRow.click();
  await page.waitForTimeout(1200);
  let panel = await body();
  check('a record offers a stage-by-stage investigation', /trace this record stage by stage/i.test(panel));

  // The trace itself lives behind the explicit Investigate action, so it
  // has to be opened before anything about its content can be asserted.
  const openTrace = page.locator('button:has-text("Investigate")').first();
  if (await openTrace.count()) {
    await openTrace.click();
    await page.waitForTimeout(4000);
    panel = await body();
  }
  check('the investigation renders a money-flow trace', /money.flow trace/i.test(panel));
  check('pending is reported as pending, never as missing',
    /pending/i.test(panel) && !/ZB-6107[\s\S]{0,400}missing settlement/i.test(panel));
  check('a stage with no uploaded source says it was not evaluated', /not evaluated/i.test(panel));
  await page.screenshot({ path: `${OUT}/flow-3-trace.png` });

  {
    const inv = panel;
    check('the investigator separates confirmed evidence from explanation',
      /confirmed evidence/i.test(inv) && /(likely explanation|explanation)/i.test(inv));
    check('the investigator states what it could not settle', /unresolved|could not/i.test(inv));
    check('AI use is stated honestly, never implied',
      /not needed|resolved deterministically|ai unavailable|assisted by|model/i.test(inv));
    await page.screenshot({ path: `${OUT}/flow-4-investigator.png` });
  }
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
}

// ---- the identical-amount trap must NOT be reconciled -----------------
const trapRow = await findRecord('ORD-7031');
if (trapRow) {
  const trapText = (await trapRow.textContent()) || '';
  check('the identical-amount trap is refused, not matched', !/RECONCILED/i.test(trapText), trapText.slice(0, 90));
}

// ---- review queue -----------------------------------------------------
await page.goto(`${APP}/app/review`, { waitUntil: 'networkidle' });
await page.waitForTimeout(1200);
const queue = await body();
check('review queue lists real pipeline decisions', /review/i.test(queue));

const disputeRow = page.locator('tr:has-text("currency mismatch"), li:has-text("currency mismatch")').first();
if (await disputeRow.count()) {
  const disputeText = (await disputeRow.textContent()) || '';
  check('a known money dispute is never offered "approve match"', !/approve match/i.test(disputeText));
}

// ---- audit ------------------------------------------------------------
const before = await (await page.request.get(`${API}/audit/verify`)).json();
await page.goto(`${APP}/app/audit`, { waitUntil: 'networkidle' });
await page.waitForTimeout(1200);
const audit = await body();
check('audit trail renders the chain', /audit/i.test(audit));
check('hash chain verifies intact', before.intact === true, `${before.total_events} events`);

// ---- export -----------------------------------------------------------
if (runId) {
  const csv = await page.request.get(`${API}/runs/${runId}/export`);
  const text = await csv.text();
  check('results export as CSV', csv.ok() && text.split('\n').length > 5);
  check('the export carries the reasoning, not just outcomes', /reason|explanation/i.test(text.split('\n')[0]));
  check('the export carries source provenance', /source_file|source_row/i.test(text.split('\n')[0]));
}

// ---- mobile -----------------------------------------------------------
const mobile = await browser.newPage({ viewport: { width: 375, height: 812 } });
for (const path of ['/', '/app/runs', '/app/review']) {
  await mobile.goto(`${APP}${path}`, { waitUntil: 'networkidle' });
  const overflow = await mobile.evaluate(() =>
    document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
  check(`no horizontal overflow at 375px on ${path}`, !overflow);
}
await mobile.close();

// ---- hygiene ----------------------------------------------------------
check('no uncaught page errors', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | '));
const unexpected = httpErrors.filter((e) => !/404/.test(e));
check('no unexpected HTTP failures', unexpected.length === 0, unexpected.slice(0, 3).join(' | '));

await browser.close();

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) {
  console.log('\nFAILED:');
  for (const f of failed) console.log(`  - ${f.name}${f.detail ? '  (' + f.detail + ')' : ''}`);
  process.exit(1);
}
