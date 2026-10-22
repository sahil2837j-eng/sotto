#!/usr/bin/env node
// Renders the staged Sotto demo frame by frame (deterministic, no dropped frames).
//   npm i playwright
//   node record.cjs --stills   -> a handful of key moments, for checking
//   node record.cjs            -> all frames into /tmp/opencode/demo-frames
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const OUT = '/tmp/opencode/demo-frames';
const STILLS = '/tmp/opencode/demo-stills';
const FPS = 20;
const DURATION = 12.6;

(async () => {
  const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1 });
  await page.goto('file://' + path.resolve(__dirname, 'index.html') + '?record', { waitUntil: 'load' });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(500);

  if (process.argv.includes('--stills')) {
    fs.mkdirSync(STILLS, { recursive: true });
    for (const t of [300, 1700, 3200, 5200, 7600, 9400, 10200, 11800]) {
      await page.evaluate((ms) => window.renderAt(ms), t);
      await page.screenshot({ path: path.join(STILLS, `t${t}.png`) });
      console.log('still', t);
    }
  } else {
    fs.rmSync(OUT, { recursive: true, force: true });
    fs.mkdirSync(OUT, { recursive: true });
    const n = Math.round(DURATION * FPS);
    for (let i = 0; i < n; i++) {
      await page.evaluate((ms) => window.renderAt(ms), (i * 1000) / FPS);
      await page.screenshot({ path: path.join(OUT, `f${String(i).padStart(4, '0')}.png`) });
      if (i % 50 === 0) process.stdout.write(`\rframes ${i}/${n}`);
    }
    process.stdout.write(`\rframes ${n}/${n}\n`);
  }
  await browser.close();
})();
