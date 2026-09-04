/**
 * End-to-end verification of the console against a real browser.
 *
 *   node e2e/verify-ui.mjs
 *
 * Requires the backend on :8000 and the dev server on :5173, and a
 * generated dataset. It resets the database first, so point it at a
 * development instance only.
 *
 * This exists because several defects in this project were invisible to
 * unit tests and to curl: a route shadowed by a sibling, a response
 * shape that differed between its found and not-found cases, an audit
 * view that rendered nothing next to a chain reporting hundreds of
 * events, and a header that pushed the page 193px sideways on a phone.
 * All four were found by driving the actual UI.
 *
 * Playwright's own browser download needs privileged install on this
 * machine, so the Chromium binary is located rather than downloaded:
 * set PLAYWRIGHT_CHROMIUM to override, otherwise the newest cached
 * ms-playwright Chromium is used, and failing that the system's
 * Chrome/Brave.
 */
import { existsSync, readdirSync } from 'node:fs';
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
  throw new Error('playwright not found; run `npx playwright --version` once to populate the cache');
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

const { chromium } = await import(findPlaywright());
const EXECUTABLE = findChromium();


const OUT = process.env.SCREENSHOT_DIR || '/tmp';
const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`);
}

await fetch('http://localhost:8000/admin/reset', { method: 'POST' });

const browser = await chromium.launch({ executablePath: EXECUTABLE });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

const consoleErrors = [];
page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
page.on('pageerror', e => consoleErrors.push('pageerror: ' + e.message));
const httpErrors = [];
page.on('response', r => { if (r.status() >= 400) httpErrors.push(`${r.status()} ${r.url()}`); });

await page.goto('http://localhost:5173', { waitUntil: 'networkidle' });

// 1. Empty state
const bodyText = await page.textContent('body');
check('page renders (not white screen)', bodyText.length > 200, `${bodyText.length} chars`);
check('empty state shown before any batch', bodyText.includes('No records yet'));
check('provenance banner present', bodyText.toLowerCase().includes('synthetic data'));
check('banner explains live API is empty', bodyText.includes('zero records'));
await page.screenshot({ path: `${OUT}/ui-1-empty.png` });

// 2. Batch selection + execution
await page.selectOption('.batch-controls select:first-of-type', 'dev');
// A batch large enough to have an observable middle. The deterministic
// backend processes ~100 records faster than this script can poll, so a
// small batch would make the progress assertion vacuous rather than passing.
await page.selectOption('.batch-controls select:nth-of-type(2)', '1000');
check('batch controls selectable', true);

await page.click('button:has-text("Run batch")');

// 3. Live progress must come from the backend, mid-run
let sawPartial = false, partialText = '';
for (let i = 0; i < 400; i++) {
  const label = await page.textContent('.progress-label').catch(() => '');
  const m = label.match(/(\d+)\s*\/\s*(\d+) processed/);
  if (m && +m[1] > 0 && +m[1] < +m[2]) { sawPartial = true; partialText = label.trim(); break; }
  if (label.includes('complete')) break;
  await page.waitForTimeout(15);
}
check('live progress observed mid-batch (real SSE, not a jump to 100%)', sawPartial, partialText);
await page.screenshot({ path: `${OUT}/ui-2-progress.png` });

await page.waitForFunction(() => document.body.innerText.includes('complete'), { timeout: 60000 });
const afterRun = await page.textContent('body');
check('batch completes', afterRun.includes('complete'));

await page.waitForFunction(() => document.querySelectorAll('.records-row').length > 0, { timeout: 15000 }).catch(() => {});
const rowCount = await page.locator('.records-row').count();
check('records table populated', rowCount > 0, `${rowCount} rows`);
await page.screenshot({ path: `${OUT}/ui-3-complete.png` });

// 4. Outcome filters
for (const label of ['EXCEPTION', 'HUMAN REVIEW', 'RECONCILED']) {
  await page.click(`.filter-row button:has-text("${label}")`);
  await page.waitForTimeout(350);
  const n = await page.locator('.records-row').count();
  const badges = await page.locator('.records-row .badge').allTextContents();
  const consistent = badges.every(b => b.trim() === label);
  check(`filter ${label} returns only matching rows`, consistent, `${n} rows`);
}
await page.click('.filter-row button:has-text("All")');
await page.waitForTimeout(300);

// 5. Record detail
await page.locator('.records-row').first().click();
await page.waitForSelector('.detail-panel', { timeout: 10000 });
const detail = await page.textContent('.detail-panel');
check('record detail opens', detail.length > 100);
check('detail shows merchant side', /merchant/i.test(detail));
check('detail shows deterministic checks', /currency_match|gross_amount_match|reference_match/.test(detail));
check('detail shows policy threshold', /threshold|0\.85/i.test(detail));
check('detail shows audit history', /audit/i.test(detail));
await page.screenshot({ path: `${OUT}/ui-4-detail.png` });
await page.click('.detail-close');
await page.waitForTimeout(300);

// 6. Exception explanation specifically
await page.click('.filter-row button:has-text("EXCEPTION")');
await page.waitForTimeout(400);
if (await page.locator('.records-row').count()) {
  await page.locator('.records-row').first().click();
  await page.waitForSelector('.detail-panel');
  const ex = await page.textContent('.detail-panel');
  check('exception record explains why it failed', /FAIL|exceed|does not|mismatch|No corresponding/i.test(ex));
  await page.screenshot({ path: `${OUT}/ui-5-exception.png` });
  await page.click('.detail-close');
}

// 7. Audit trail + chain integrity
await page.click('nav >> text=Audit');
await page.waitForTimeout(1200);
const audit = await page.textContent('body');
check('audit trail lists events', /RECORD_DECIDED|BATCH_STARTED/.test(audit));
check('chain integrity reported', /intact|verified|chain/i.test(audit));
await page.screenshot({ path: `${OUT}/ui-6-audit.png` });

// 8. Responsive
await page.setViewportSize({ width: 390, height: 844 });
await page.click('nav >> text=Console');
await page.waitForTimeout(600);
const overflow = await page.evaluate(() =>
  document.documentElement.scrollWidth - document.documentElement.clientWidth);
check('no horizontal overflow at 390px', overflow <= 2, `overflow ${overflow}px`);
await page.screenshot({ path: `${OUT}/ui-7-mobile.png` });

// 9. Error state: backend unreachable
await page.setViewportSize({ width: 1440, height: 900 });
await page.route('**/batch/**', r => r.abort());
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForTimeout(1500);
const errText = await page.textContent('body');
check('survives backend failure without white screen', errText.length > 200, `${errText.length} chars`);
await page.screenshot({ path: `${OUT}/ui-8-error.png` });

const realHttpErrors = httpErrors.filter(e => !/favicon|\.svg|batch/i.test(e));
check('no unexpected HTTP errors', realHttpErrors.length === 0, realHttpErrors.slice(0, 3).join(' | ') || 'none');

await browser.close();
const failed = results.filter(r => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) { console.log('FAILED:'); failed.forEach(f => console.log(`  - ${f.name} ${f.detail}`)); process.exit(1); }
