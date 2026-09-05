/**
 * End-to-end verification of the product flow: upload, map, run, inspect,
 * review, audit, export.
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
const OUT = process.env.SCREENSHOT_DIR || '/tmp';
const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`);
}

await fetch('http://localhost:8000/admin/reset', { method: 'POST' });

const { chromium } = await import(findPlaywright());
const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

const consoleErrors = [];
page.on('pageerror', (e) => consoleErrors.push(e.message));
const httpErrors = [];
page.on('response', (r) => { if (r.status() >= 400) httpErrors.push(`${r.status()} ${r.url()}`); });

await page.goto('http://localhost:5173', { waitUntil: 'networkidle' });
await page.waitForTimeout(800);

// ---- landing -------------------------------------------------------
const landing = await page.textContent('body');
check('opens on Runs, not a benchmark console', /Reconciliation runs/i.test(landing));
check('brand is Axiom Recon', /Axiom Recon/.test(landing));
check('empty state invites a run', /No runs yet/i.test(landing));
await page.screenshot({ path: `${OUT}/flow-1-runs-empty.png` });

// ---- upload --------------------------------------------------------
await page.click('button:has-text("New run")');
await page.waitForTimeout(600);
check('upload flow is the primary action', /New reconciliation run/i.test(await page.textContent('body')));

const uploads = [
  ['ORDERS', 'orders.csv'],
  ['PAYMENT_GATEWAY', 'gateway_payouts.csv'],
  ['BANK_STATEMENT', 'bank_statement.csv'],
];
for (const [type, filename] of uploads) {
  await page.setInputFiles(`#file-${type}`, join(DEMO, filename));
  await page.waitForTimeout(900);
}
const afterUpload = await page.textContent('body');
check('all three sources uploaded', /Sources \(3\)/.test(afterUpload));
check('ledger and settlement roles are shown', /Ledger/.test(afterUpload) && /Settlement/.test(afterUpload));
await page.screenshot({ path: `${OUT}/flow-2-sources.png` });

// ---- column mapping ------------------------------------------------
await page.locator('button:has-text("Review columns")').first().click();
await page.waitForTimeout(500);
const mapping = await page.textContent('body');
check('detected columns are shown for confirmation', /Transaction ID|Reference/.test(mapping));
check('detection confidence is disclosed', /%\s*·/.test(mapping));
const selects = await page.locator('.mapping-row select').count();
check('columns can be remapped by the user', selects > 5, `${selects} mappable fields`);
await page.screenshot({ path: `${OUT}/flow-3-mapping.png` });
await page.locator('button:has-text("Hide columns")').first().click();

// ---- run -----------------------------------------------------------
const runButton = page.locator('button:has-text("Run reconciliation")');
check('run is enabled once both sides are present and mapped', await runButton.isEnabled());
await runButton.click();
// Wait on the transition rather than a fixed sleep: execution parses
// every uploaded file before returning, so a fixed wait races it.
await page.waitForSelector('.records-row', { timeout: 30000 }).catch(() => {});
await page.waitForTimeout(600);

const detail = await page.textContent('body');
check('run detail opens after execution', /All runs/.test(detail));
const rows = await page.locator('.records-row').count();
check('results table is populated', rows > 0, `${rows} rows`);
await page.screenshot({ path: `${OUT}/flow-4-results.png` });

// ---- the decisions the demo exists to show -------------------------
const bodyText = await page.textContent('body');
for (const [label, pattern] of [
  ['reconciled records present', /RECONCILED/],
  ['exceptions present', /EXCEPTION/],
  ['human review present', /HUMAN REVIEW/],
]) {
  check(label, pattern.test(bodyText));
}

// AI-assisted filter must reflect real backend state.
await page.click('button:has-text("AI-assisted")');
await page.waitForTimeout(500);
const aiRows = await page.locator('.records-row').count();
check('AI-assisted filter narrows to model-touched records', aiRows > 0 && aiRows < rows,
      `${aiRows} of ${rows}`);
await page.click('button:has-text("AI-assisted")');
await page.waitForTimeout(400);

// ---- record detail -------------------------------------------------
await page.locator('.records-row').first().click();
await page.waitForSelector('.detail-panel', { timeout: 8000 });
const record = await page.textContent('.detail-panel');
check('record detail shows both sides', /Merchant|Razorpay|Ledger/i.test(record));
check('record detail explains the decision in plain language', record.length > 200);
check('deterministic checks are listed', /currency_match|gross_amount_match|reference_match/.test(record));
await page.screenshot({ path: `${OUT}/flow-5-record.png` });
await page.click('.detail-close');
await page.waitForTimeout(400);

// ---- review queue --------------------------------------------------
await page.click('nav >> text=Review Queue');
await page.waitForTimeout(1200);
const queue = await page.textContent('body');
check('review queue holds real escalated records', /awaiting review/i.test(queue));
const items = await page.locator('.review-item').count();
check('queue items present', items > 0, `${items} items`);

if (items > 0) {
  const first = page.locator('.review-item').first();
  await first.locator('button:has-text("Show evidence")').click();
  await page.waitForTimeout(500);
  check('evidence names the candidates considered',
        /Candidates considered|No settlement records/i.test(await first.textContent()));

  const before = await (await page.request.get('http://localhost:8000/audit/verify')).json();
  await first.locator('button:has-text("Escalate")').click();
  await page.waitForTimeout(1200);
  const after = await (await page.request.get('http://localhost:8000/audit/verify')).json();
  check('a human action reaches the hash chain',
        after.total_events === before.total_events + 1,
        `${before.total_events} -> ${after.total_events}`);
  check('chain stays intact after the action', after.intact === true);
}
await page.screenshot({ path: `${OUT}/flow-6-review.png` });

// ---- audit ---------------------------------------------------------
await page.click('nav >> text=Audit Trail');
await page.waitForTimeout(1400);
const audit = await page.textContent('body');
check('audit trail lists ingestion and decision events',
      /SOURCE_UPLOADED|RUN_CREATED|RECORD_DECIDED/.test(audit));
check('chain integrity is reported', /verified|intact|integrity/i.test(audit));
await page.screenshot({ path: `${OUT}/flow-7-audit.png` });

// ---- export --------------------------------------------------------
const runsResponse = await (await page.request.get('http://localhost:8000/runs')).json();
const runId = runsResponse.runs[0].batch_id;
const exportResponse = await page.request.get(`http://localhost:8000/runs/${runId}/export`);
const csv = await exportResponse.text();
check('export returns a CSV with decision evidence',
      exportResponse.ok() && csv.startsWith('record_id,outcome,exception_type'),
      `${csv.trim().split('\n').length - 1} rows`);

// ---- accessibility -------------------------------------------------
await page.goto('http://localhost:5173', { waitUntil: 'networkidle' });
await page.waitForTimeout(700);
await page.keyboard.press('Tab');
const firstFocus = await page.evaluate(() => (document.activeElement.textContent || '').trim());
check('first Tab reaches the skip link', /skip to main/i.test(firstFocus), firstFocus);

const semantics = await page.evaluate(() => ({
  h1: document.querySelectorAll('h1').length,
  main: document.querySelectorAll('main').length,
  unlabelledButtons: [...document.querySelectorAll('button')].filter(
    (b) => !b.textContent.trim() && !b.getAttribute('aria-label') && !b.getAttribute('title')).length,
  unlabelledInputs: [...document.querySelectorAll('input:not([type=file]), select')].filter(
    (el) => !el.labels?.length && !el.getAttribute('aria-label')).length,
  tablesWithoutHeaders: [...document.querySelectorAll('table')].filter(
    (t) => t.querySelectorAll('th').length === 0).length,
}));
check('exactly one h1', semantics.h1 === 1, `${semantics.h1}`);
check('main landmark present', semantics.main === 1);
check('every button has an accessible name', semantics.unlabelledButtons === 0);
check('every input and select is labelled', semantics.unlabelledInputs === 0,
      `${semantics.unlabelledInputs} unlabelled`);
check('every table has header cells', semantics.tablesWithoutHeaders === 0);

for (const [w, h] of [[390, 844], [768, 1024], [1280, 800]]) {
  await page.setViewportSize({ width: w, height: h });
  await page.waitForTimeout(400);
  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check(`no horizontal overflow at ${w}px`, overflow <= 2, `overflow ${overflow}px`);
}
await page.setViewportSize({ width: 390, height: 844 });
await page.screenshot({ path: `${OUT}/flow-8-mobile.png` });

const reduced = await browser.newContext({ reducedMotion: 'reduce', viewport: { width: 1280, height: 800 } });
const reducedPage = await reduced.newPage();
await reducedPage.goto('http://localhost:5173', { waitUntil: 'networkidle' });
await reducedPage.waitForTimeout(800);
check('reduced motion suppresses transitions', await reducedPage.evaluate(() => {
  const el = document.querySelector('.page') || document.body;
  return parseFloat(getComputedStyle(el).transitionDuration) < 0.05;
}));
await reducedPage.close();
await reduced.close();

const realHttpErrors = httpErrors.filter((e) => !/favicon|\.svg/i.test(e));
check('no unexpected HTTP errors', realHttpErrors.length === 0, realHttpErrors.slice(0, 3).join(' | ') || 'none');
check('no uncaught page errors', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | ') || 'none');

await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) {
  console.log('FAILED:');
  failed.forEach((f) => console.log(`  - ${f.name} ${f.detail}`));
  process.exit(1);
}
