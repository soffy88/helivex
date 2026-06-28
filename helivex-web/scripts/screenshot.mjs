/**
 * screenshot.mjs — capture full-page PNGs of each dashboard tab for visual review.
 *
 * Usage:  node scripts/screenshot.mjs [outDir] [baseUrl]
 *   outDir   default ./screenshots
 *   baseUrl  default http://localhost:3400
 *
 * Requires the dev/prod server running. Headless Chromium needs the nss libs
 * (libnss3/libnspr4); on a minimal box without them:
 *   apt-get download libnss3 libnspr4 && dpkg -x <deb> ./_nss
 *   LD_LIBRARY_PATH=./_nss/usr/lib/x86_64-linux-gnu node scripts/screenshot.mjs
 */
import { mkdir } from 'node:fs/promises';
import { chromium } from 'playwright';

const OUT = process.argv[2] ?? './screenshots';
const BASE = process.argv[3] ?? 'http://localhost:3400';

const TABS = ['overview', 'portfolio', 'risk', 'micro', 'backtest', 'executions', 'pnl', 'audit'];

await mkdir(OUT, { recursive: true });
const browser = await chromium.launch({ args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
for (const tab of TABS) {
  const url = tab === 'overview' ? `${BASE}/` : `${BASE}/?tab=${tab}`;
  await page.goto(url, { waitUntil: 'networkidle', timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(1800);
  const h = await page.evaluate(() => document.body.scrollHeight);
  await page.screenshot({ path: `${OUT}/${tab}.png`, fullPage: true });
  console.log(`${tab.padEnd(11)} ${h}px → ${OUT}/${tab}.png`);
}
await browser.close();
