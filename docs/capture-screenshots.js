/**
 * MEMPAS Demo Screenshot Capture
 * Uses puppeteer-core + local Chrome to capture all pages.
 * Run: node docs/capture-screenshots.js
 */
const puppeteer = require('puppeteer-core');
const path = require('path');
const fs = require('fs');

const DIR = path.join(__dirname, 'demo-screenshots');
const BASE = 'http://localhost:3000';
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

const PAGES = [
  { name: '01-login',              url: '/login',           wait: 1500 },
  { name: '02-dashboard-top',      url: '/dashboard',       wait: 3000 },
  { name: '03-dashboard-heatmap',  url: '/dashboard',       wait: 3000, scroll: 500 },
  { name: '04-materials',          url: '/materials',       wait: 4000 },
  { name: '05-suppliers',          url: '/suppliers',       wait: 3000 },
  { name: '06-compare-config',     url: '/compare',         wait: 2000 },
  { name: '08-invite',             url: '/invite',          wait: 2000 },
  { name: '09-history',            url: '/analysis',        wait: 3000 },
  { name: '10-settings-weights',   url: '/system/settings', wait: 2000 },
  { name: '11-settings-thresholds',url: '/system/settings', wait: 2000, clickTab: 1 },
];

(async () => {
  if (!fs.existsSync(DIR)) fs.mkdirSync(DIR, { recursive: true });

  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--window-size=1920,1080'],
    defaultViewport: { width: 1920, height: 1080 },
  });

  const page = await browser.newPage();

  // Login: set localStorage token directly
  await page.goto(`${BASE}/login`, { waitUntil: 'networkidle2', timeout: 10000 });
  await page.evaluate(() => {
    localStorage.setItem('mempas_token', 'mempas-demo-' + Date.now());
    localStorage.setItem('mempas_user', JSON.stringify({
      username: 'admin', nickname: '管理员', role: 'admin'
    }));
  });
  console.log('Login token set.');

  for (const p of PAGES) {
    process.stdout.write(`[${p.name}] `);
    try {
      await page.goto(`${BASE}${p.url}`, { waitUntil: 'networkidle2', timeout: 15000 });
    } catch {
      // networkidle may timeout on heavy pages, continue anyway
      console.log('(slow load) ');
    }
    await new Promise(r => setTimeout(r, p.wait || 2000));

    if (p.scroll) {
      await page.evaluate((y) => window.scrollTo(0, y), p.scroll);
      await new Promise(r => setTimeout(r, 800));
    }

    if (p.clickTab !== undefined) {
      const tabs = await page.$$('.ant-tabs-tab');
      if (tabs[p.clickTab]) {
        await tabs[p.clickTab].click();
        await new Promise(r => setTimeout(r, 1000));
      }
    }

    const fp = path.join(DIR, `${p.name}.jpg`);
    await page.screenshot({ path: fp, type: 'jpeg', quality: 92 });
    const size = (fs.statSync(fp).size / 1024).toFixed(1);
    console.log(`${p.name}.jpg (${size} KB)`);
  }

  await browser.close();
  const files = fs.readdirSync(DIR).filter(f => f.endsWith('.jpg'));
  console.log(`\nDone! ${files.length} screenshots in ${DIR}`);
})();
