/**
 * The four defects a finance person hits in the first minute.
 *
 * 1. Export — findable, CSV and XLSX, scoped to the filter on screen.
 * 2. A one-sided workspace, and a junk file, refused with a readable reason.
 * 3. The review queue's header count and its list length agreeing.
 * 4. Triage chips readable by someone who has never seen the engine.
 *
 * Driven in a real browser because every one of these is a rendering
 * question: a green build says nothing about whether the button exists.
 *
 * Does NOT reset the database. It reads the run that is already there and
 * creates draft workspaces for the refusal cases; drafts are hidden from
 * the run list and no longer capture the review queue.
 */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';

const API = process.env.ACCORD_API || 'http://localhost:8000';
const APP = process.env.ACCORD_APP || 'http://localhost:5173';
const FIXTURES = process.env.ACCORD_FIXTURES;
const OUT = process.env.SCREENSHOT_DIR || '/tmp';

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

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`);
}

const { chromium } = await import(findPlaywright());
const browser = await chromium.launch({ executablePath: findChromium() });
const context = await browser.newContext({
  viewport: { width: 1440, height: 950 },
  acceptDownloads: true,
});
const page = await context.newPage();

const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(e.message));
const httpErrors = [];
page.on('response', (r) => {
  if (r.status() >= 400) httpErrors.push(`${r.status()} ${r.url()}`);
});

const body = () => page.textContent('body');

// ---------------------------------------------------------------- the run
const listed = await (await page.request.get(`${API}/runs`)).json();
const run = (listed.runs || [])[0];
if (!run) {
  console.error('no executed run on this backend — load the sample workspace first');
  process.exit(2);
}
const runId = run.batch_id;
const counts = run.outcome_counts || {};
console.log(`\ndriving run ${runId}: ${JSON.stringify(counts)}\n`);

await page.goto(`${APP}/app/runs/${runId}`, { waitUntil: 'networkidle' });
await page.waitForTimeout(1800);

// ============================================================ 1. EXPORT ==
const detail = await body();
check('export is on the run detail without opening a menu', /Export all records/i.test(detail));

// Unmissable means visible in the first screenful, next to the figures.
const exportBox = await page.locator('.wk-export').first().boundingBox();
check(
  'the export control sits with the results summary, above the fold',
  !!exportBox && exportBox.y < 950,
  exportBox ? `y=${Math.round(exportBox.y)}px` : 'not rendered'
);

const csvLink = page.locator('.wk-export a:has-text("CSV")').first();
const xlsxLink = page.locator('.wk-export a:has-text("Excel")').first();
check('both formats are offered, not just CSV',
  (await csvLink.count()) > 0 && (await xlsxLink.count()) > 0);

async function download(locator) {
  const wait = page.waitForEvent('download', { timeout: 30000 });
  await locator.click();
  const dl = await wait;
  const path = await dl.path();
  return { name: dl.suggestedFilename(), path, size: statSync(path).size };
}

/**
 * Count CSV records, not lines.
 *
 * Explanations legitimately contain newlines inside quoted fields, so
 * splitting on \n over-counts — which is a bug in the check, not in the
 * file, and would have been reported as a data-loss defect.
 */
function csvRecords(text) {
  let rows = 0;
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i];
    if (c === '"') {
      if (quoted && text[i + 1] === '"') i += 1;
      else quoted = !quoted;
    } else if (c === '\n' && !quoted) rows += 1;
  }
  if (text.length && !text.endsWith('\n')) rows += 1;
  return rows;
}

const csvFile = await download(csvLink);
const csvText = readFileSync(csvFile.path, 'utf8');

check('the CSV downloads as a file', csvFile.size > 0 && /\.csv$/.test(csvFile.name),
  `${csvFile.name} ${csvFile.size}B`);
const csvHeader = csvText.slice(0, csvText.indexOf('\n'));
check('the CSV holds every record in the run',
  csvRecords(csvText) - 1 === (run.total_records || 0),
  `${csvRecords(csvText) - 1} records vs ${run.total_records} in the run`);
check('the CSV carries the evidence, not just verdicts',
  /reason/.test(csvHeader) && /explanation/.test(csvHeader) &&
  /ledger_source_file/.test(csvHeader) && /settlement_source_file/.test(csvHeader),
  csvHeader.slice(0, 90));

const xlsxFile = await download(xlsxLink);
const xlsxBytes = readFileSync(xlsxFile.path);
check('the XLSX downloads as a real workbook',
  xlsxBytes.slice(0, 4).toString('binary') === 'PK\x03\x04' && /\.xlsx$/.test(xlsxFile.name),
  `${xlsxFile.name} ${xlsxFile.size}B`);

// The scope has to follow the filter, or the file and the table disagree.
await page.locator('.wk-outcomes button:has-text("Exceptions")').first().click();
await page.waitForTimeout(1200);
const filtered = await body();
check('the export renames itself to the filter on screen',
  /Export exceptions/i.test(filtered), (filtered.match(/Export \w+[\w ]*/) || [])[0]);

const scopedCsv = await download(page.locator('.wk-export a:has-text("CSV")').first());
const scopedCount = csvRecords(readFileSync(scopedCsv.path, 'utf8')) - 1;
check('the filtered export contains exactly what the filter shows',
  scopedCount === (counts.EXCEPTION || 0),
  `${scopedCount} records vs ${counts.EXCEPTION} exceptions`);
check('the filtered file is named for its scope', /exceptions/.test(scopedCsv.name), scopedCsv.name);

const scopedXlsx = await download(page.locator('.wk-export a:has-text("Excel")').first());
check('the filtered XLSX downloads too',
  readFileSync(scopedXlsx.path).slice(0, 4).toString('binary') === 'PK\x03\x04', scopedXlsx.name);

await page.screenshot({ path: `${OUT}/x1-export.png`, fullPage: false });

// ================================================== 4. RUN-DETAIL CHIPS ==
// Every chip carries its meaning as a tooltip; the row shows the selected
// one as a visible sentence. "Exceptions" is selected at this point.
const outcomeTips = await page.locator('.wk-outcomes .wk-outcome-pill').evaluateAll((els) =>
  els.map((e) => e.getAttribute('title') || ''));
check('every outcome chip carries a plain-language explanation',
  outcomeTips.length >= 4 && outcomeTips.every((t) => t.length > 25),
  `${outcomeTips.filter((t) => t.length > 25).length}/${outcomeTips.length}`);
check('the visible helper line explains the selected outcome',
  /the money disagrees/i.test(filtered),
  (filtered.match(/A settlement was found[^.]{0,80}/i) || [])[0]);
await page.locator('.wk-outcomes button:has-text("Reconciled")').first().click();
await page.waitForTimeout(900);
check('the helper line follows the outcome filter',
  /agreed on every check/i.test(await body()));
await page.locator('.wk-outcomes button:has-text("Exceptions")').first().click();
await page.waitForTimeout(900);

// ============================================== 3. REVIEW QUEUE COUNTS ==
await page.goto(`${APP}/app/review`, { waitUntil: 'networkidle' });
await page.waitForTimeout(2000);

const queueApi = await (await page.request.get(`${API}/review/queue?limit=500`)).json();
const openTotal = queueApi.total;
let queue = await body();

const headerText = (await page.locator('.pn-section-title').first().textContent()) || '';
const listRows = await page.locator('.pn-case').count();
const railOpen = (await page.locator('.pn-metric').first().textContent()) || '';

check('the queue header states its scope against the real open total',
  new RegExp(`of\\s+${openTotal}\\s+open`, 'i').test(headerText),
  `header="${headerText.trim()}" total=${openTotal}`);
check('the header count and the list length agree',
  headerText.includes(String(listRows)) && listRows === openTotal,
  `${listRows} rows rendered, ${openTotal} open`);
check('the summary rail and the list do not contradict each other',
  railOpen.includes(String(openTotal)),
  `rail="${railOpen.replace(/\s+/g, ' ').trim()}"`);

// ================================================== 4. TRIAGE CHIPS =====
const chips = await page.locator('.pn-chip').allTextContents();
const chipLabels = chips.map((c) => c.replace(/\s+/g, ' ').trim());
console.log('   chips:', chipLabels.join(' | '));
check('no chip is named in engine vocabulary',
  !chipLabels.some((c) => /differ|ambiguous|unmatched|missing evidence|money dispute/i.test(c)),
  chipLabels.join(' | '));
check('chips name the problem in finance language',
  chipLabels.some((c) => /Amount doesn/i.test(c)) &&
  chipLabels.some((c) => /No settlement found/i.test(c)) &&
  chipLabels.some((c) => /Several possible matches/i.test(c)) &&
  chipLabels.some((c) => /Waiting on settlement/i.test(c)));
check('no chip is shown with a zero count',
  !chipLabels.some((c) => /\b0$/.test(c)), chipLabels.join(' | '));
check('a visible line explains the selected chip',
  /Every record still waiting on a person/i.test(queue));

const tooltips = await page.locator('.pn-chip').evaluateAll((els) =>
  els.map((e) => e.getAttribute('title') || ''));
check('every chip carries a one-line explanation as a tooltip',
  tooltips.every((t) => t.length > 20), `${tooltips.filter((t) => t.length > 20).length}/${tooltips.length}`);

// Selecting a chip keeps the two numbers honest.
const amountChip = page.locator('.pn-chip:has-text("Amount doesn")').first();
const amountCount = Number(((await amountChip.textContent()) || '').match(/(\d+)\s*$/)?.[1] || 0);
await amountChip.click();
await page.waitForTimeout(900);
const afterChip = (await page.locator('.pn-section-title').first().textContent()) || '';
const afterRows = await page.locator('.pn-case').count();
check('filtering by a chip keeps the header and the list in step',
  afterRows === amountCount && afterChip.includes(String(amountCount)),
  `chip=${amountCount} rows=${afterRows} header="${afterChip.trim()}"`);
check('the helper line follows the selected chip',
  /figures on it disagree with the ledger/i.test(await body()));

await page.screenshot({ path: `${OUT}/x2-queue.png`, fullPage: false });

// ---- the queue export -------------------------------------------------
check('the review queue offers an export', await page.locator('.pn-export').count() > 0);
const queueCsv = await download(page.locator('.pn-export a:has-text("CSV")').first());
const queueCount = csvRecords(readFileSync(queueCsv.path, 'utf8')) - 1;
check('the queue export is the whole queue, not the page',
  queueCount === openTotal, `${queueCount} records vs ${openTotal} open`);
const queueXlsx = await download(page.locator('.pn-export a:has-text("Excel")').first());
check('the queue exports to Excel too',
  readFileSync(queueXlsx.path).slice(0, 4).toString('binary') === 'PK\x03\x04', queueXlsx.name);

// ---- an action decrements BOTH numbers --------------------------------
await page.locator('.pn-chip:has-text("All open")').first().click();
await page.waitForTimeout(700);
const beforeAction = await page.locator('.pn-case').count();
await page.locator('.pn-case .pn-btn:not(.pn-btn-quiet)').first().click();
await page.waitForTimeout(2500);
const afterApi = await (await page.request.get(`${API}/review/queue?limit=500`)).json();
const afterRowsCount = await page.locator('.pn-case').count();
const afterHeader = (await page.locator('.pn-section-title').first().textContent()) || '';
const afterRail = (await page.locator('.pn-metric').first().textContent()) || '';
check('an action removes the record from the list',
  afterRowsCount === beforeAction - 1, `${beforeAction} -> ${afterRowsCount}`);
check('the header count decrements with it',
  afterHeader.includes(String(afterApi.total)), `header="${afterHeader.trim()}" total=${afterApi.total}`);
check('the summary rail decrements with it',
  afterRail.includes(String(afterApi.total)) && afterApi.total === openTotal - 1,
  `rail="${afterRail.replace(/\s+/g, ' ').trim()}"`);

// ======================================= 2. ONE-SIDED / JUNK WORKSPACE ==
if (FIXTURES) {
  // ---- only a bank statement: one side, so nothing to reconcile against
  await page.goto(`${APP}/app/runs/new`, { waitUntil: 'networkidle' });
  await page.setInputFiles('#wk-file-input', [join(FIXTURES, 'hdfc_bank_statement_march.csv')]);
  await page.waitForTimeout(4000);
  const oneSided = await body();
  check('a one-sided workspace says reconciliation needs two sides',
    /Reconciliation needs two sides/i.test(oneSided));
  check('it names what is present and what is missing',
    /ledger side/i.test(oneSided) && /orders export/i.test(oneSided) &&
    /accounting or ERP export/i.test(oneSided));
  check('it lists the file that IS there',
    /hdfc_bank_statement_march\.csv/i.test(oneSided));
  const runLabel = (await page.locator('.wk-btn-run').first().textContent()) || '';
  check('the run button is blocked and says why',
    /Blocked/i.test(runLabel) && /ledger/i.test(runLabel), runLabel.trim());
  const disabled = await page.locator('.wk-btn-run').first().isDisabled();
  check('the blocked button cannot be pressed', disabled);
  await page.screenshot({ path: `${OUT}/x3-onesided.png`, fullPage: true });

  // ---- a junk CSV alongside it: flagged per file, with a reason ---------
  await page.setInputFiles('#wk-file-input', [join(FIXTURES, 'top-100-movies.csv')]);
  await page.waitForTimeout(4000);
  const junk = await body();
  check('the junk file is named, not lumped into a generic error',
    /top-100-movies\.csv/i.test(junk));
  check('the junk file says exactly what it is missing',
    /cannot reconcile[\s\S]{0,140}(no amount column|no date column)/i.test(junk),
    (junk.match(/cannot reconcile[^.]{0,120}/i) || [])[0]);
  check('it says what it DID read from the file',
    /rank, title, director/i.test(junk) || /read \d+ columns from this file/i.test(junk));
  check('it offers Remove on that file',
    (await page.locator('button:has-text("Remove top-100-movies.csv")').count()) > 0);
  check('the junk file shows a status, not a silent "accepted"',
    /No amount or date|No amount column|No date column|No rows read/i.test(junk));
  check('a required column guessed off a nonsense header is stated, not buried',
    /It did read [^.]*as the amount column/i.test(junk),
    (junk.match(/It did read [^.]{0,70}/i) || [])[0] || 'nothing was mapped');
  check('the good file beside it is still marked ready', /Ready/i.test(junk));
  await page.screenshot({ path: `${OUT}/x4-junk.png`, fullPage: true });

  // Removing it takes the blocker with it.
  await page.locator('button:has-text("Remove top-100-movies.csv")').first().click();
  await page.waitForTimeout(2500);
  check('removing the junk file takes its row out of the inventory',
    (await page.locator('button:has-text("Remove top-100-movies.csv")').count()) === 0);

  // ---- a file that is not a spreadsheet at all -------------------------
  await page.setInputFiles('#wk-file-input', [join(FIXTURES, 'notes.txt')]);
  await page.waitForTimeout(3500);
  const nonTable = await body();
  check('a non-spreadsheet upload is reported per file with a reason',
    /notes\.txt/i.test(nonTable) &&
    /(no columns|is this a CSV|headers but no rows|could not be read)/i.test(nonTable),
    (nonTable.match(/notes\.txt[^.]{0,80}/) || [])[0]);
  await page.screenshot({ path: `${OUT}/x5-nontable.png`, fullPage: true });
} else {
  console.log('   (skipped upload refusals — set ACCORD_FIXTURES)');
}

// ------------------------------------------------------------- hygiene --
const mobile = await context.newPage();
for (const path of [`/app/runs/${runId}`, '/app/review']) {
  await mobile.setViewportSize({ width: 375, height: 812 });
  await mobile.goto(`${APP}${path}`, { waitUntil: 'networkidle' });
  await mobile.waitForTimeout(1200);
  const overflow = await mobile.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1
  );
  check(`no horizontal overflow at 375px on ${path}`, !overflow);
}
await mobile.close();

check('no uncaught page errors', pageErrors.length === 0, pageErrors.slice(0, 2).join(' | '));
// A refused upload IS a 400 — that is the product working, and the check
// is that the screen explains it, which is asserted above.
const unexpected = httpErrors.filter((e) => !/404/.test(e) && !/400 .*\/sources$/.test(e));
check('no unexpected HTTP failures', unexpected.length === 0, unexpected.slice(0, 3).join(' | '));

await browser.close();

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) {
  console.log('\nFAILED:');
  for (const f of failed) console.log(`  - ${f.name}${f.detail ? '  (' + f.detail + ')' : ''}`);
  process.exit(1);
}
