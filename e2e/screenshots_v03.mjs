#!/usr/bin/env node
/**
 * Echoic v0.3 UI 截图与视觉巡检（Playwright + Chromium）
 *
 * 覆盖 v0.3 新界面：
 *   01-library-dark.png      资料库 · 深色主题
 *   02-detail-dark.png       详情页 · 深色主题（观点树 + AI 面板）
 *   03-trash.png             资料库 · 回收站（保留期/恢复/彻底删除）
 *   04-live-captions.png     录音页 · 实时字幕（Beta，mock provider）
 *   05-semantic-search.png   全局搜索 · 混合/语义模式
 *   06-settings-index.png    设置 · 检索索引卡片（后端/模型/已索引/重建）
 *   07-light-library.png     资料库 · 浅色主题（对照）
 *   08-mobile-dark.png       资料库 · 深色主题 · 移动端 390x844
 *
 * 运行（服务已在 :8100 运行，前端 dist 已构建）：
 *   cd e2e
 *   export HOME=/home/wpcwzy/echoic/.npm-home
 *   export PLAYWRIGHT_BROWSERS_PATH=/home/wpcwzy/echoic/.cache/ms-playwright
 *   ECHOIC_E2E_BASE_URL=http://127.0.0.1:8100 node screenshots_v03.mjs
 *
 * 退出码：0 = 截图齐全（允许 WARN）；1 = 关键页面/截图失败。
 */
import { chromium } from 'playwright';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..');
const OUT_DIR = process.env.ECHOIC_E2E_SCREENSHOT_DIR || resolve(REPO_ROOT, 'docs/screenshots/v0.3');
const BASE_URL = (process.env.ECHOIC_E2E_BASE_URL || 'http://127.0.0.1:8100').replace(/\/$/, '');
const ADMIN_USER = process.env.ECHOIC_E2E_ADMIN_USERNAME || process.env.ECHOIC_ADMIN_USERNAME || 'admin';
const ADMIN_PASS = process.env.ECHOIC_E2E_ADMIN_PASSWORD || process.env.ECHOIC_ADMIN_PASSWORD || 'admin123';
const VIEWPORT = { width: 1680, height: 1050 };
const THEME_STORAGE_KEY = 'echoic:theme-mode';

const consoleErrors = [];
const pageErrors = [];
const failedRequests = [];
const overflowIssues = [];
const themeChecks = [];
const savedShots = [];
const warnings = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function log(msg) { console.log(`[shots-v03] ${msg}`); }
function warn(msg) { warnings.push(msg); console.log(`\x1b[1;33m[WARN]\x1b[0m ${msg}`); }
function ok(msg) { console.log(`\x1b[1;32m[OK]\x1b[0m ${msg}`); }

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
        const loc = page.getByText(pattern, { exact: false }).first();
        if (await loc.isVisible()) return pattern;
      } catch { /* ignore */ }
    }
    await sleep(250);
  }
  throw new Error(`等待文本超时：${patterns.join(' / ')}`);
}

async function clickByText(page, patterns, timeout = 8000) {
  for (const pattern of patterns) {
    for (const loc of [
      page.getByRole('button', { name: pattern }).first(),
      page.getByRole('menuitem', { name: pattern }).first(),
      page.getByRole('tab', { name: pattern }).first(),
      page.getByText(pattern, { exact: true }).first(),
      page.locator(`text=${pattern}`).first(),
    ]) {
      try {
        if ((await loc.count()) > 0 && (await loc.first().isVisible())) {
          await loc.first().click({ timeout });
          return pattern;
        }
      } catch { /* try next */ }
    }
  }
  throw new Error(`未找到可点击元素：${patterns.join(' / ')}`);
}

async function api(page, method, path, data) {
  const opts = { method };
  if (data !== undefined) opts.data = data;
  const resp = await page.request.fetch(`${BASE_URL}${path}`, opts);
  const text = await resp.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch { /* not json */ }
  return { status: resp.status(), json, headers: resp.headers() };
}

async function login(page, username = ADMIN_USER, password = ADMIN_PASS) {
  await page.goto(`${BASE_URL}/login`, { waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('networkidle').catch(() => {});
  await sleep(700);
  const userField = page.locator('input[placeholder*="用户"], input[placeholder*="账号"]').first();
  const fallback = page.locator('input:not([type="password"])').first();
  await ((await userField.count()) > 0 ? userField : fallback).fill(username);
  await page.locator('input[type="password"]').first().fill(password);
  await clickByText(page, [/登\s*录/]);
  await page.waitForURL((u) => !u.pathname.startsWith('/login'), { timeout: 20000 });
  await page.waitForLoadState('networkidle').catch(() => {});
  await sleep(800);
}

async function openUserMenu(page) {
  const candidates = [
    page.locator('.app-topbar__user').first(),
    page.locator('[class*="user-menu-trigger"]').first(),
    page.locator('.app-topbar .ant-dropdown-trigger').last(),
    page.getByRole('button', { name: /管理员|成员|用户菜单|账号/ }).first(),
  ];
  for (const loc of candidates) {
    try {
      if ((await loc.count()) > 0 && (await loc.first().isVisible())) {
        await loc.first().click({ timeout: 5000 });
        await sleep(500);
        return true;
      }
    } catch { /* try next */ }
  }
  return false;
}

async function setThemeViaUI(page, label) {
  const opened = await openUserMenu(page);
  if (!opened) throw new Error('未找到用户菜单入口');
  try {
    await clickByText(page, [/外观/]);
    await sleep(400);
  } catch { /* 某些版本可能直接展示主题项 */ }
  await clickByText(page, [label]);
  await sleep(800);
  return true;
}

async function themeState(page) {
  return page.evaluate(() => {
    const root = document.documentElement;
    const bodyBg = getComputedStyle(document.body).backgroundColor;
    const shell = document.querySelector('.app-shell');
    const shellBg = shell ? getComputedStyle(shell).backgroundColor : bodyBg;
    const parse = (value) => {
      const match = String(value).match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)/);
      if (!match) return null;
      return [Number(match[1]), Number(match[2]), Number(match[3])];
    };
    const luminance = (rgb) => (rgb ? (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255 : null);
    const bodyRgb = parse(bodyBg);
    const shellRgb = parse(shellBg);
    return {
      dataTheme: root.getAttribute('data-theme'),
      colorScheme: root.style.colorScheme,
      stored: localStorage.getItem('echoic:theme-mode'),
      bodyBg,
      shellBg,
      bodyLum: luminance(bodyRgb),
      shellLum: luminance(shellRgb),
    };
  });
}

function checkTheme(page, label, expected) {
  return themeState(page).then((state) => {
    themeChecks.push({ page: label, expected, ...state });
    if (state.dataTheme !== expected) warn(`[${label}] data-theme=${state.dataTheme}，期望 ${expected}`);
    if (expected === 'dark' && state.bodyLum !== null && state.bodyLum > 0.55) {
      warn(`[${label}] 深色模式页面背景偏亮（lum=${state.bodyLum?.toFixed(2)}，${state.bodyBg}）`);
    }
    if (expected === 'light' && state.bodyLum !== null && state.bodyLum < 0.35) {
      warn(`[${label}] 浅色模式页面背景偏暗（lum=${state.bodyLum?.toFixed(2)}，${state.bodyBg}）`);
    }
    return state;
  });
}

async function checkOverflow(page, label) {
  try {
    const metrics = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    }));
    if (metrics.scrollWidth > metrics.innerWidth + 2) {
      overflowIssues.push({ page: label, ...metrics });
      warn(`[${label}] 横向溢出 scrollWidth=${metrics.scrollWidth} > innerWidth=${metrics.innerWidth}`);
    } else {
      log(`[${label}] 无横向溢出`);
    }
  } catch (err) { warn(`[${label}] 溢出检测失败：${err.message}`); }
}

async function createReadyRecording(page, title) {
  // 用 ffmpeg 生成 6s 16k mono wav，再通过 API multipart 上传（自包含，不依赖已有数据）
  const dir = mkdtempSync(join(tmpdir(), 'echoic-v03-shot-'));
  const wavPath = join(dir, 'fixture.wav');
  const ffmpeg = process.env.ECHOIC_FFMPEG || 'ffmpeg';
  const proc = spawnSync(ffmpeg, [
    '-hide_banner', '-loglevel', 'error', '-y',
    '-f', 'lavfi', '-i', 'sine=frequency=330:duration=6',
    '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', wavPath,
  ], { encoding: 'utf-8' });
  if (proc.status !== 0) throw new Error(`ffmpeg 生成截图素材失败：${proc.stderr || proc.error}`);
  const buffer = readFileSync(wavPath);
  const resp = await page.request.fetch(`${BASE_URL}/api/recordings/upload`, {
    method: 'POST',
    multipart: {
      file: { name: 'qa-ui-fixture.wav', mimeType: 'audio/wav', buffer },
      title: title,
      language: 'zh',
    },
  });
  if (resp.status() !== 201) throw new Error(`上传截图素材失败：HTTP ${resp.status()} ${await resp.text()}`);
  const rec = await resp.json();
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    const detail = await (await page.request.fetch(`${BASE_URL}/api/recordings/${rec.id}`)).json();
    if (detail.status === 'ready') return detail;
    if (detail.status === 'failed') throw new Error(`截图素材处理失败：${detail.error_message}`);
    await sleep(800);
  }
  throw new Error('截图素材处理超时');
}

function attachDiagnostics(page, label) {
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push({ page: label, text: msg.text() });
  });
  page.on('pageerror', (err) => pageErrors.push({ page: label, error: String(err && err.stack ? err.stack : err).split('\n')[0] }));
  page.on('requestfailed', (req) => {
    if (req.url().includes('/api/')) failedRequests.push({ page: label, url: req.url(), failure: req.failure()?.errorText });
  });
  page.on('response', (resp) => {
    if (resp.url().includes('/api/') && resp.status() >= 500) failedRequests.push({ page: label, url: resp.url(), status: resp.status() });
  });
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  log(`目标服务：${BASE_URL}`);
  log(`输出目录：${OUT_DIR}`);
  let browser;
  let exitCode = 0;
  const contexts = [];
  try {
    browser = await chromium.launch({
      headless: true,
      args: [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--font-render-hinting=none',
        '--use-fake-ui-for-media-stream',
        '--use-fake-device-for-media-stream',
        '--autoplay-policy=no-user-gesture-required',
      ],
    });
    const newContext = async (opts = {}) => {
      const ctx = await browser.newContext({
        viewport: VIEWPORT,
        deviceScaleFactor: 1,
        locale: 'zh-CN',
        timezoneId: 'Asia/Shanghai',
        colorScheme: 'light',
        ...opts,
      });
      contexts.push(ctx);
      return ctx;
    };

    // ===================== 管理员上下文 =====================
    const adminCtx = await newContext();
    await adminCtx.grantPermissions(['microphone']);
    const page = await adminCtx.newPage();
    attachDiagnostics(page, 'admin');
    await login(page);
    ok('管理员登录成功');

    // 向量索引前置：mock embeddings + reindex（设置页/搜索页截图需要）
    try {
      await api(page, 'PUT', '/api/settings', { ai: { embeddings: { provider: 'mock' } } });
      const reindex = await api(page, 'POST', '/api/vector/reindex', { force: true });
      log(`vector reindex: HTTP ${reindex.status} ${JSON.stringify(reindex.json)?.slice(0, 160)}`);
    } catch (err) { warn(`向量索引准备失败：${err.message}`); }

    // 回收站截图素材：自包含生成 6s wav → 上传 → 处理完成 → 软删除
    let trashId = null;
    try {
      const fixture = await createReadyRecording(page, 'QA 回收站示例录音');
      await api(page, 'DELETE', `/api/recordings/${fixture.id}`);
      trashId = fixture.id;
      log(`回收站素材：已上传并软删除录音 ${fixture.id}（${fixture.title}）`);
    } catch (err) { warn(`回收站素材准备失败：${err.message}`); }

    // ---------------- 07 浅色资料库（对照） ----------------
    log('—— 07 浅色资料库 ——');
    try {
      await page.evaluate((key) => localStorage.setItem(key, 'light'), THEME_STORAGE_KEY).catch(() => {});
      await page.goto(`${BASE_URL}/`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(page, [/我的录音库/, /资料库/], 15000);
      await sleep(1000);
      await checkTheme(page, 'library-light', 'light');
      await shot(page, '07-light-library.png', '资料库 · 浅色主题');
      await checkOverflow(page, 'library-light');
    } catch (err) {
      warn(`浅色资料库截图失败：${err.message}`);
      await shot(page, '07-light-library.png', '资料库 · 浅色（异常状态下截图）');
    }

    // ---------------- 深色主题（UI 菜单切换 + 持久化） ----------------
    log('—— 深色主题切换（用户菜单） ——');
    try {
      await page.goto(`${BASE_URL}/`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await sleep(800);
      try {
        await setThemeViaUI(page, '深色');
        ok('通过用户菜单切换为深色');
      } catch (err) {
        warn(`用户菜单切换深色失败，回退 localStorage：${err.message}`);
        await page.evaluate((key) => localStorage.setItem(key, 'dark'), THEME_STORAGE_KEY);
        await page.reload({ waitUntil: 'domcontentloaded' });
        await sleep(800);
      }
      await checkTheme(page, 'after-toggle', 'dark');
      // 刷新验证持久化
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await sleep(900);
      const persisted = await checkTheme(page, 'after-reload', 'dark');
      if (persisted.stored !== 'dark') warn(`主题持久化异常：localStorage=${persisted.stored}`);
    } catch (err) { warn(`深色主题切换异常：${err.message}`); }

    // ---------------- 01 深色资料库 ----------------
    log('—— 01 深色资料库 ——');
    try {
      await page.goto(`${BASE_URL}/`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(page, [/我的录音库/, /资料库/], 15000);
      await sleep(1200);
      await shot(page, '01-library-dark.png', '资料库 · 深色主题');
      await checkOverflow(page, 'library-dark');
    } catch (err) {
      warn(`深色资料库截图失败：${err.message}`);
      await shot(page, '01-library-dark.png', '资料库 · 深色（异常状态下截图）');
    }

    // ---------------- 02 深色详情页 ----------------
    log('—— 02 深色详情页 ——');
    try {
      const listed = await api(page, 'GET', '/api/recordings?page_size=10');
      const demo = (listed.json?.items || []).find((item) => String(item.title || '').includes('TraceOpt')) || (listed.json?.items || [])[0];
      if (!demo) throw new Error('无录音可打开详情页');
      await page.goto(`${BASE_URL}/r/${demo.id}`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(page, [/AI问答/, /AI 问答/, /转写/, /观点树/], 20000);
      await sleep(1400);
      await clickByText(page, [/观点树/]).catch(() => {});
      await sleep(800);
      await checkTheme(page, 'detail-dark', 'dark');
      await shot(page, '02-detail-dark.png', '详情页 · 深色主题（观点树 + AI 面板）');
      await checkOverflow(page, 'detail-dark');
    } catch (err) {
      warn(`深色详情页截图失败：${err.message}`);
      await shot(page, '02-detail-dark.png', '详情页 · 深色（异常状态下截图）');
    }

    // ---------------- 03 回收站 ----------------
    log('—— 03 回收站 ——');
    try {
      await page.goto(`${BASE_URL}/?trashed=1`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(page, [/回收站/], 15000);
      await sleep(1200);
      await shot(page, '03-trash.png', '资料库 · 回收站（保留期提示 / 恢复 / 彻底删除）');
      await checkOverflow(page, 'trash');
    } catch (err) {
      warn(`回收站截图失败：${err.message}`);
      await shot(page, '03-trash.png', '回收站（异常状态下截图）');
    }
    void trashId;

    // ---------------- 04 实时字幕（录屏页） ----------------
    log('—— 04 实时字幕（Beta） ——');
    try {
      await page.goto(`${BASE_URL}/record`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await sleep(1500);
      const features = await api(page, 'GET', '/api/system/features');
      const liveEnabled = Boolean(features.json?.live?.enabled);
      if (!liveEnabled) warn('features.live.enabled=false，实时字幕入口不可见');
      await clickByText(page, [/开始录音/, /开始录制/]);
      await sleep(2000);
      try {
        await waitForAnyText(page, [/实时转写中|连接中|实时字幕/], 15000);
        // 等待若干实时字幕行（mock provider 按音频时长渐进输出）
        const deadline = Date.now() + 28000;
        let lines = 0;
        while (Date.now() < deadline) {
          const meta = await page.locator('.live-captions__meta').first().innerText().catch(() => '');
          lines = Number((meta.match(/(\d+)\s*段已确定/) || [])[1] || 0);
          if (lines >= 2) break;
          await sleep(600);
        }
        log(`实时字幕：${lines} 段已确定`);
        if (lines < 2) warn(`实时字幕仅 ${lines} 段已确定（期望 >=2）`);
      } catch (err) {
        warn(`实时字幕等待超时：${err.message}`);
      }
      await shot(page, '04-live-captions.png', '录音页 · 实时字幕（Beta，mock provider）');
      await checkOverflow(page, 'live-captions');
      try {
        await page.locator('button[aria-label="结束录音"], button[aria-label="停止录音"]').first().click({ timeout: 5000 });
      } catch {
        await page.getByText(/结束录音|停止录音|停止并保存/).first().click({ timeout: 5000 }).catch((err) => warn(`停止录音按钮未找到：${err.message}`));
      }
      await sleep(1500);
    } catch (err) {
      warn(`实时字幕截图失败：${err.message}`);
      await shot(page, '04-live-captions.png', '录音页 · 实时字幕（异常状态下截图）');
    }

    // ---------------- 05 语义/混合搜索 ----------------
    log('—— 05 语义搜索 ——');
    try {
      await page.goto(`${BASE_URL}/search?q=${encodeURIComponent('摘要 两周 交付节点')}`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await sleep(1200);
      try {
        // 精确点击 Segmented 的「语义检索」选项（避免命中页面说明文字）
        await page.getByText('语义检索', { exact: true }).first().click({ timeout: 5000 });
        await sleep(700);
        const selectedMode = await page.locator('.ant-segmented-item-selected').first().innerText().catch(() => '');
        log(`搜索模式：${selectedMode}`);
        // 切换 Segmented 本身即会用当前 query 重新请求向量搜索（见 SearchPage changeMode）
        await sleep(2500);
        await waitForAnyText(page, [/找到 \d+ 条结果|语义检索 ·|语义相关的内容|降级为关键词/], 15000);
        const bodyText = await page.locator('body').innerText();
        if (!/找到 \d+ 条结果|相关度/.test(bodyText)) warn('语义搜索结果区未出现结果/相关度文本');
        else log('语义搜索结果已渲染');
      } catch (err) {
        warn(`搜索模式切换/结果加载未完成：${err.message}`);
        try {
          const bodyText = await page.locator('body').innerText();
          log(`搜索页当前状态：${bodyText.replace(/\n+/g, ' | ').slice(0, 260)}`);
        } catch { /* ignore */ }
      }
      await page.evaluate(() => window.scrollTo(0, 0)).catch(() => {});
      await sleep(400);
      await shot(page, '05-semantic-search.png', '全局搜索 · 混合/语义模式（score + 高亮）');
      await checkOverflow(page, 'semantic-search');
    } catch (err) {
      warn(`语义搜索截图失败：${err.message}`);
      await shot(page, '05-semantic-search.png', '语义搜索（异常状态下截图）');
    }

    // ---------------- 06 设置 · 检索索引 ----------------
    log('—— 06 设置 · 检索索引 ——');
    try {
      await page.goto(`${BASE_URL}/settings?tab=system`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(page, [/系统/, /检索索引/, /向量/], 15000);
      await waitForAnyText(page, [/检索索引（向量搜索）|检索索引/], 15000).catch((err) => warn(`检索索引卡片未找到：${err.message}`));
      const indexCard = page.getByText(/检索索引（向量搜索）/).first();
      await indexCard.scrollIntoViewIfNeeded().catch(() => {});
      await page.evaluate(() => window.scrollBy(0, -120)).catch(() => {});
      await sleep(1200);
      await checkTheme(page, 'settings-dark', 'dark');
      await shot(page, '06-settings-index.png', '设置 · 检索索引（后端/模型/已索引/重建）');
      await checkOverflow(page, 'settings-index');
    } catch (err) {
      warn(`设置索引页截图失败：${err.message}`);
      await shot(page, '06-settings-index.png', '设置 · 检索索引（异常状态下截图）');
    }

    // ---------------- 08 移动端深色 ----------------
    log('—— 08 移动端深色 ——');
    try {
      const mobileCtx = await newContext({ viewport: { width: 390, height: 844 }, colorScheme: 'dark', isMobile: true, hasTouch: true });
      const mobilePage = await mobileCtx.newPage();
      attachDiagnostics(mobilePage, 'mobile-dark');
      await login(mobilePage);
      await mobilePage.evaluate((key) => localStorage.setItem(key, 'dark'), THEME_STORAGE_KEY);
      await mobilePage.goto(`${BASE_URL}/`, { waitUntil: 'domcontentloaded' });
      await mobilePage.waitForLoadState('networkidle').catch(() => {});
      await sleep(1400);
      await checkTheme(mobilePage, 'mobile-dark', 'dark');
      await shot(mobilePage, '08-mobile-dark.png', '资料库 · 深色 · 移动端 390x844');
      await checkOverflow(mobilePage, 'mobile-dark');
    } catch (err) {
      warn(`移动端深色截图失败：${err.message}`);
      try {
        await shot(page, '08-mobile-dark.png', '移动端深色（异常状态下截图）');
      } catch { /* ignore */ }
    }

    // ===================== 诊断汇总 =====================
    const expectedConsole = consoleErrors.filter((e) => /401|403|404/.test(e.text) || /Failed to load resource/.test(e.text));
    const unexpectedConsole = consoleErrors.filter((e) => !expectedConsole.includes(e));
    console.log('\n================ v0.3 视觉巡检汇总 ================');
    console.log(`截图数量：${savedShots.length}`);
    for (const item of savedShots) console.log(`  ${item.filename}  ${item.note}`);
    console.log(`主题检查：${themeChecks.length} 次`);
    for (const item of themeChecks) console.log(`  - ${item.page}: data-theme=${item.dataTheme} stored=${item.stored} bodyLum=${item.bodyLum?.toFixed?.(2)}`);
    console.log(`控制台错误：${consoleErrors.length}（预期 401/403 噪声 ${expectedConsole.length}）`);
    for (const item of expectedConsole.slice(0, 6)) console.log(`  - [预期] ${item.page}: ${item.text}`);
    for (const item of unexpectedConsole.slice(0, 10)) console.log(`  - [异常] ${item.page}: ${item.text}`);
    console.log(`未捕获页面异常：${pageErrors.length}`);
    for (const item of pageErrors.slice(0, 5)) console.log(`  - ${item.page}: ${item.error}`);
    console.log(`失败请求（>=500/网络错误）：${failedRequests.length}`);
    for (const item of failedRequests.slice(0, 8)) console.log(`  - ${item.status || ''} ${item.url} ${item.failure || ''}`);
    console.log(`横向溢出问题：${overflowIssues.length}`);
    for (const item of overflowIssues) console.log(`  - ${item.page}: ${item.scrollWidth} > ${item.innerWidth}`);
    console.log(`告警：${warnings.length}`);
    for (const item of warnings) console.log(`  - ${item}`);

    writeFileSync(
      resolve(REPO_ROOT, 'e2e', 'screenshot-report-v03.json'),
      JSON.stringify({ baseUrl: BASE_URL, viewport: VIEWPORT, savedShots, themeChecks, consoleErrors, pageErrors, failedRequests, overflowIssues, warnings }, null, 2),
      'utf-8',
    );

    const required = ['01-library-dark.png', '02-detail-dark.png', '03-trash.png', '04-live-captions.png',
      '05-semantic-search.png', '06-settings-index.png', '07-light-library.png', '08-mobile-dark.png'];
    const missing = required.filter((name) => !savedShots.some((s) => s.filename === name));
    if (missing.length) { console.error(`\x1b[1;31m[FAIL]\x1b[0m 关键截图缺失：${missing.join(', ')}`); exitCode = 1; }
    if (pageErrors.length > 0) warn(`存在 ${pageErrors.length} 条未捕获页面异常`);
    if (unexpectedConsole.length > 0) warn(`存在 ${unexpectedConsole.length} 条非预期控制台错误`);
    console.log(exitCode === 0 ? '\x1b[1;32mv0.3 截图任务：PASS ✔\x1b[0m' : '\x1b[1;31mv0.3 截图任务：FAIL\x1b[0m');
  } catch (err) {
    console.error(`\x1b[1;31m[FATAL]\x1b[0m ${err.stack || err}`);
    exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  process.exit(exitCode);
}

main();
