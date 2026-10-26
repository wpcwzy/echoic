#!/usr/bin/env node
/**
 * Echoic UI 截图与视觉巡检（Playwright + Chromium）
 *
 * 前置条件：
 *   1) 服务已以生产式启动（推荐 scripts/run-local.sh，端口 8100）；
 *   2) 已安装 Playwright 与 chromium：
 *        cd e2e
 *        export HOME=<repo>/.npm-home
 *        export npm_config_cache=<repo>/.cache/npm
 *        export npm_config_logs_dir=<repo>/.cache/npm-logs
 *        npm install
 *        PLAYWRIGHT_BROWSERS_PATH=<repo>/.cache/ms-playwright npx playwright install chromium
 *
 * 运行：
 *   PLAYWRIGHT_BROWSERS_PATH=... node e2e/screenshots.mjs
 *   ECHOIC_E2E_BASE_URL=http://127.0.0.1:8100 node e2e/screenshots.mjs
 *
 * 产物（1680x1050 视口）：
 *   docs/screenshots/01-login.png             登录页（未登录）
 *   docs/screenshots/02-library.png           资料库（演示数据）
 *   docs/screenshots/03-detail-viewpoint.png  详情页 · 观点树页签 + 右侧 AI 面板
 *   docs/screenshots/04-detail-transcript.png 详情页 · 转写页签
 *   docs/screenshots/05-mindmap.png           思维导图弹窗
 *   docs/screenshots/06-settings.png          设置页
 *
 * 退出码：0 = 截图齐全（允许个别 WARN）；1 = 登录/关键页面失败或关键截图缺失。
 */
import { chromium } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..');
const OUT_DIR = process.env.ECHOIC_E2E_SCREENSHOT_DIR || resolve(REPO_ROOT, 'docs/screenshots');
const BASE_URL = (process.env.ECHOIC_E2E_BASE_URL || 'http://127.0.0.1:8100').replace(/\/$/, '');
const USERNAME = process.env.ECHOIC_E2E_ADMIN_USERNAME || process.env.ECHOIC_ADMIN_USERNAME || 'admin';
const PASSWORD = process.env.ECHOIC_E2E_ADMIN_PASSWORD || process.env.ECHOIC_ADMIN_PASSWORD || 'admin123';
const VIEWPORT = { width: 1680, height: 1050 };

const consoleErrors = [];
const pageErrors = [];
const failedRequests = [];
const overflowIssues = [];
const savedShots = [];
const warnings = [];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function log(msg) {
  console.log(`[shots] ${msg}`);
}
function warn(msg) {
  warnings.push(msg);
  console.log(`\x1b[1;33m[WARN]\x1b[0m ${msg}`);
}
function ok(msg) {
  console.log(`\x1b[1;32m[OK]\x1b[0m ${msg}`);
}

async function shot(page, filename, note = '') {
  const filePath = resolve(OUT_DIR, filename);
  try {
    await page.screenshot({ path: filePath, animations: 'disabled' });
    savedShots.push({ filename, path: filePath, note });
    ok(`截图 ${filename}${note ? `（${note}）` : ''}`);
    return true;
  } catch (err) {
    warn(`截图 ${filename} 失败：${err.message}`);
    return false;
  }
}

async function waitForAnyText(page, patterns, timeout = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    for (const pattern of patterns) {
      try {
        const locator = page.getByText(pattern, { exact: false }).first();
        if (await locator.isVisible()) return pattern;
      } catch {
        /* ignore */
      }
    }
    await sleep(250);
  }
  throw new Error(`等待文本超时：${patterns.join(' / ')}`);
}

async function clickByText(page, patterns, timeout = 8000) {
  for (const pattern of patterns) {
    const candidates = [
      page.getByRole('button', { name: pattern }).first(),
      page.getByRole('tab', { name: pattern }).first(),
      page.getByText(pattern, { exact: true }).first(),
      page.locator('.ant-tabs-tab', { hasText: pattern }).first(),
      page.locator(`text=${pattern}`).first(),
    ];
    for (const locator of candidates) {
      try {
        if ((await locator.count()) > 0 && (await locator.first().isVisible())) {
          await locator.first().click({ timeout });
          return pattern;
        }
      } catch {
        /* try next */
      }
    }
  }
  throw new Error(`未找到可点击元素：${patterns.join(' / ')}`);
}

async function clickTab(page, label) {
  const tabs = [
    page.getByRole('tab', { name: label }).first(),
    page.locator('.ant-tabs-tab', { hasText: label }).first(),
    page.locator('.ant-segmented-item', { hasText: label }).first(),
    page.getByText(label, { exact: true }).first(),
  ];
  for (const tab of tabs) {
    try {
      if ((await tab.count()) > 0 && (await tab.first().isVisible())) {
        await tab.first().click({ timeout: 6000 });
        await sleep(700);
        return label;
      }
    } catch {
      /* try next */
    }
  }
  throw new Error(`未找到页签：${label}`);
}

async function checkOverflow(page, label) {
  try {
    const metrics = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      bodyScrollWidth: document.body ? document.body.scrollWidth : 0,
      innerWidth: window.innerWidth,
    }));
    if (metrics.scrollWidth > metrics.innerWidth + 2) {
      overflowIssues.push({ page: label, ...metrics, note: '页面出现横向滚动（内容溢出视口）' });
      warn(`[${label}] 横向溢出：scrollWidth=${metrics.scrollWidth} > innerWidth=${metrics.innerWidth}`);
    } else {
      log(`[${label}] 无横向溢出（scrollWidth=${metrics.scrollWidth}, innerWidth=${metrics.innerWidth}）`);
    }
  } catch (err) {
    warn(`[${label}] 溢出检测失败：${err.message}`);
  }
}

async function login(page) {
  await page.goto(`${BASE_URL}/login`, { waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('networkidle').catch(() => {});
  await sleep(800);
  await waitForAnyText(page, [/登录/, /欢迎/, /Echoic/], 15000);
  return true;
}

async function doLogin(page) {
  const usernameInput = page
    .locator('input[placeholder*="用户"], input[placeholder*="账号"], input[placeholder*="用户名"]')
    .first();
  const fallbackFirst = page.locator('input:not([type="password"])').first();
  const userField = (await usernameInput.count()) > 0 ? usernameInput : fallbackFirst;
  await userField.fill(USERNAME);
  const passwordField = page.locator('input[type="password"]').first();
  await passwordField.fill(PASSWORD);
  await clickByText(page, [/登\s*录/, /登录/]);
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 20000 });
  await page.waitForLoadState('networkidle').catch(() => {});
  await sleep(1200);
  return true;
}

async function getDemoRecordingId(page) {
  for (const query of ['TraceOpt', '']) {
    const resp = await page.request.get(`${BASE_URL}/api/recordings?page=1&page_size=50&query=${encodeURIComponent(query)}`);
    if (!resp.ok()) continue;
    const data = await resp.json();
    const items = data.items || [];
    const demo = items.find((item) => String(item.title || '').includes('TraceOpt')) ||
      items.find((item) => item.status === 'ready') ||
      items[0];
    if (demo && demo.id) return demo.id;
  }
  throw new Error('无法通过 API 找到演示录音 id');
}

function attachDiagnostics(page) {
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push({ text: msg.text(), location: msg.location() });
  });
  page.on('pageerror', (err) => pageErrors.push(String(err && err.stack ? err.stack : err)));
  page.on('requestfailed', (req) => {
    const url = req.url();
    if (url.includes('/api/')) failedRequests.push({ url, failure: req.failure() ? req.failure().errorText : 'unknown' });
  });
  page.on('response', (resp) => {
    const url = resp.url();
    if (url.includes('/api/') && resp.status() >= 500) {
      failedRequests.push({ url, status: resp.status(), failure: 'HTTP >= 500' });
    }
  });
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  log(`目标服务：${BASE_URL}`);
  log(`输出目录：${OUT_DIR}`);
  log(`视口：${VIEWPORT.width}x${VIEWPORT.height}`);

  let browser;
  let exitCode = 0;
  try {
    browser = await chromium.launch({
      headless: true,
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--font-render-hinting=none'],
    });
    const context = await browser.newContext({
      viewport: VIEWPORT,
      deviceScaleFactor: 1,
      locale: 'zh-CN',
      timezoneId: 'Asia/Shanghai',
      colorScheme: 'light',
    });
    const page = await context.newPage();
    attachDiagnostics(page);

    // ---------------- 01 登录页 ----------------
    log('—— 01 登录页 ——');
    try {
      await login(page);
      await shot(page, '01-login.png', '登录页 + admin/admin123 提示');
      await checkOverflow(page, 'login');
    } catch (err) {
      warn(`登录页加载失败：${err.message}`);
      exitCode = 1;
    }

    // ---------------- 登录 ----------------
    log('—— 执行登录 ——');
    try {
      await doLogin(page);
      ok(`登录成功（${USERNAME}），当前 URL=${page.url()}`);
    } catch (err) {
      console.error(`\x1b[1;31m[FAIL]\x1b[0m 登录失败：${err.message}`);
      await page.screenshot({ path: resolve(OUT_DIR, '00-login-failed.png') }).catch(() => {});
      exitCode = 1;
    }

    // ---------------- 02 资料库 ----------------
    log('—— 02 资料库 ——');
    try {
      await page.goto(`${BASE_URL}/`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(page, [/我的录音库/, /资料库/, /录音库/], 15000);
      await sleep(1200);
      await shot(page, '02-library.png', '资料库：统计卡 + 录音卡片 + 演示数据');
      await checkOverflow(page, 'library');
    } catch (err) {
      warn(`资料库页异常：${err.message}`);
      await shot(page, '02-library.png', '资料库（异常状态下截图）');
    }

    // ---------------- 03/04/05 录音详情 ----------------
    let demoId = null;
    try {
      demoId = await getDemoRecordingId(page);
      log(`演示录音 id=${demoId}`);
    } catch (err) {
      warn(`获取演示录音失败：${err.message}`);
    }

    if (demoId) {
      log('—— 03 详情页 · 观点树 + AI 面板 ——');
      try {
        await page.goto(`${BASE_URL}/r/${demoId}`, { waitUntil: 'domcontentloaded' });
        await page.waitForLoadState('networkidle').catch(() => {});
        await waitForAnyText(page, [/AI问答/, /AI 问答/, /转写/, /观点树/], 20000);
        await sleep(1500);
        // 默认页签可能是转写，统一切到观点树（截图要求）
        await clickTab(page, '观点树').catch((err) => warn(`切换到观点树页签失败：${err.message}`));
        await sleep(900);
        await shot(page, '03-detail-viewpoint.png', '详情页：观点树页签 + 右侧 AI 面板');
        await checkOverflow(page, 'detail-viewpoint');

        log('—— 04 详情页 · 转写 ——');
        try {
          await clickTab(page, '转写');
          await sleep(900);
          await shot(page, '04-detail-transcript.png', '详情页：转写页签（说话人/时间戳/文本）');
          await checkOverflow(page, 'detail-transcript');
        } catch (err) {
          warn(`转写页签截图失败：${err.message}`);
        }

        log('—— 05 思维导图弹窗 ——');
        try {
          await clickByText(page, [/思维导图/]);
          await sleep(1800); // 等待 React Flow + dagre 布局稳定
          const modalVisible = await page
            .locator('.ant-modal-content, .ant-modal-wrap, [role="dialog"]')
            .first()
            .isVisible()
            .catch(() => false);
          if (!modalVisible) warn('思维导图弹窗未检测到（截图仍保存以便排查）');
          await shot(page, '05-mindmap.png', '思维导图弹窗（React Flow + dagre）');
          await page.keyboard.press('Escape').catch(() => {});
          await sleep(500);
        } catch (err) {
          warn(`思维导图截图失败：${err.message}`);
        }
      } catch (err) {
        warn(`详情页访问异常：${err.message}`);
        await shot(page, '03-detail-viewpoint.png', '详情页（异常状态下截图）');
      }
    } else {
      warn('未获取到演示录音 id，跳过详情页截图');
    }

    // ---------------- 06 设置页 ----------------
    log('—— 06 设置页 ——');
    try {
      await page.goto(`${BASE_URL}/settings`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(page, [/设置/, /AI 服务/, /AI服务/, /账户/], 15000);
      await sleep(1200);
      await shot(page, '06-settings.png', '设置页：AI 服务 / 提示词 / 系统 / 账户');
      await checkOverflow(page, 'settings');
    } catch (err) {
      warn(`设置页异常：${err.message}`);
      await shot(page, '06-settings.png', '设置页（异常状态下截图）');
    }

    // ---------------- 诊断汇总 ----------------
    console.log('\n================ 视觉巡检汇总 ================');
    console.log(`截图数量：${savedShots.length}/${6}`);
    for (const item of savedShots) console.log(`  已保存 ${item.filename}  ${item.note}`);
    console.log(`控制台错误：${consoleErrors.length} 条`);
    for (const item of consoleErrors.slice(0, 10)) console.log(`  - ${item.text}`);
    console.log(`未捕获页面异常：${pageErrors.length} 条`);
    for (const item of pageErrors.slice(0, 5)) console.log(`  - ${String(item).split('\n')[0]}`);
    console.log(`失败请求（>=500 / 网络错误）：${failedRequests.length} 条`);
    for (const item of failedRequests.slice(0, 10)) console.log(`  - ${item.status || ''} ${item.url} ${item.failure || ''}`);
    console.log(`横向溢出问题：${overflowIssues.length} 处`);
    for (const item of overflowIssues) console.log(`  - ${item.page}: ${item.note}（${item.scrollWidth} > ${item.innerWidth}）`);
    console.log(`告警：${warnings.length} 条`);
    for (const item of warnings) console.log(`  - ${item}`);

    writeFileSync(
      resolve(OUT_DIR, '..', '..', 'e2e', 'screenshot-report.json'),
      JSON.stringify(
        { baseUrl: BASE_URL, viewport: VIEWPORT, savedShots, consoleErrors, pageErrors, failedRequests, overflowIssues, warnings },
        null,
        2,
      ),
      'utf-8',
    );

    const required = ['01-login.png', '02-library.png', '03-detail-viewpoint.png', '06-settings.png'];
    const missing = required.filter((name) => !savedShots.some((shotItem) => shotItem.filename === name));
    if (missing.length > 0) {
      console.error(`\x1b[1;31m[FAIL]\x1b[0m 关键截图缺失：${missing.join(', ')}`);
      exitCode = 1;
    }
    if (pageErrors.length > 0) {
      warn(`存在 ${pageErrors.length} 条未捕获页面异常（详见 e2e/screenshot-report.json）`);
    }
    console.log(exitCode === 0 ? '\x1b[1;32m截图任务：PASS ✔\x1b[0m' : '\x1b[1;31m截图任务：FAIL\x1b[0m');
  } catch (err) {
    console.error(`\x1b[1;31m[FATAL]\x1b[0m ${err.stack || err}`);
    exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  process.exit(exitCode);
}

main();
