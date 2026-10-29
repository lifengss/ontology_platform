const path = require('path');

function loadPlaywright() {
  const candidates = [
    path.join(process.env.USERPROFILE, 'AppData', 'Roaming', 'npm', 'node_modules', '@playwright', 'cli', 'node_modules', 'playwright-core'),
    path.join(process.env.USERPROFILE, 'AppData', 'Roaming', 'npm', 'node_modules', 'agent-browser', 'node_modules', 'playwright-core'),
    path.join(process.env.USERPROFILE, 'AppData', 'Roaming', 'npm', 'node_modules', 'playwright-core'),
    'd:\\self_coding\\knowledgeOS\\test-knowledge-system\\node_modules\\playwright-core',
    'playwright-core',
  ];
  for (const c of candidates) {
    try { return require(c); } catch (e) { /* try next */ }
  }
  throw new Error('playwright-core not found');
}

const { chromium } = loadPlaywright();
const EXE = 'C:\\Users\\lif_lc\\.agent-browser\\browsers\\chrome-156.0.8078.73\\chrome.exe';
const SHOT = 'd:\\self_coding\\ontology_platform\\frontend\\logo-options\\easter-check.png';

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: EXE, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));
  await page.goto('http://localhost:8899/index.html', { waitUntil: 'load', timeout: 30000 });
  await page.waitForTimeout(2500);

  // T1: Alt + click logo x5 -> modal opens (real keydown first, synthetic fallback for headless quirk)
  await page.keyboard.down('Alt');
  for (let i = 0; i < 5; i++) await page.click('#brandLogo');
  await page.keyboard.up('Alt');
  let hidden1 = await page.$eval('#devEasterModal', el => el.classList.contains('hidden'));
  let t1note = 'real-alt';
  if (hidden1) {
    await page.evaluate(() => {
      const logo = document.getElementById('brandLogo');
      for (let i = 0; i < 5; i++) logo.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, altKey: true }));
    });
    hidden1 = await page.$eval('#devEasterModal', el => el.classList.contains('hidden'));
    t1note = 'synthetic-alt';
  }
  console.log('T1 modal opened (expect true):', !hidden1, '(' + t1note + ')');
  await page.screenshot({ path: SHOT });

  // T2: hover close x -> modal dodges
  const before = await page.$eval('#devEasterModal', el => el.style.left + '|' + el.style.top);
  const box = await (await page.$('#devEasterClose')).boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 3 });
  await page.waitForTimeout(600);
  const after = await page.$eval('#devEasterModal', el => el.style.left + '|' + el.style.top);
  console.log('T2 dodge (expect true):', before !== after, '(' + before + ' -> ' + after + ')');

  // T3: Esc closes
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  const hidden3 = await page.$eval('#devEasterModal', el => el.classList.contains('hidden'));
  console.log('T3 closed by Esc (expect true):', hidden3);

  if (errors.length) console.log('JS errors:', errors.slice(0, 5).join(' || '));
  await browser.close();
  process.exit(!hidden1 || before === after || !hidden3 ? 1 : 0);
})().catch(e => { console.error('FATAL:', e.message); process.exit(2); });
