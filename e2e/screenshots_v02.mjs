#!/usr/bin/env node
/**
 * Echoic v0.2 UI 截图与视觉巡检（Playwright + Chromium）
 *
 * 覆盖 v0.2 新界面：
 *   01-users.png            管理后台 · 用户管理（/users）
 *   02-share-dialog.png     详情页 · 分享弹窗（生成链接 + 已有分享列表）
 *   03-public-share.png     公开分享页（未登录，播放器 + 转写页签）
 *   04-public-password.png  公开分享页 · 密码解锁页
 *   05-consume-settings.png 设置 · 系统（开放注册 + consume 自动导入 + 立即扫描）
 *   06-member-library.png   普通成员视角资料库（不含管理入口）
 *   07-public-expired.png   公开分享 · 失效/过期提示页
 *
 * 运行（服务已在 :8100 运行，前端 dist 已构建）：
 *   cd e2e
 *   export HOME=/home/wpcwzy/echoic/.npm-home
 *   export PLAYWRIGHT_BROWSERS_PATH=/home/wpcwzy/echoic/.cache/ms-playwright
 *   ECHOIC_E2E_BASE_URL=http://127.0.0.1:8100 node screenshots_v02.mjs
 *
 * 退出码：0 = 截图齐全（允许 WARN）；1 = 关键页面/截图失败。
 */
import { chromium } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..');
const OUT_DIR = process.env.ECHOIC_E2E_SCREENSHOT_DIR || resolve(REPO_ROOT, 'docs/screenshots/v0.2');
const BASE_URL = (process.env.ECHOIC_E2E_BASE_URL || 'http://127.0.0.1:8100').replace(/\/$/, '');
const ADMIN_USER = process.env.ECHOIC_E2E_ADMIN_USERNAME || process.env.ECHOIC_ADMIN_USERNAME || 'admin';
const ADMIN_PASS = process.env.ECHOIC_E2E_ADMIN_PASSWORD || process.env.ECHOIC_ADMIN_PASSWORD || 'admin123';
const MEMBER_USER = 'qa_ui_member';
const MEMBER_PASS = 'QaUiPass123';
const VIEWPORT = { width: 1680, height: 1050 };

const consoleErrors = [];
const pageErrors = [];
const failedRequests = [];
const overflowIssues = [];
const savedShots = [];
const warnings = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function log(msg) { console.log(`[shots-v02] ${msg}`); }
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

async function checkOverflow(page, label) {
  try {
    const metrics = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      bodyWidth: document.body ? document.body.scrollWidth : 0,
    }));
    if (metrics.scrollWidth > metrics.innerWidth + 2) {
      overflowIssues.push({ page: label, ...metrics });
      warn(`[${label}] 横向溢出 scrollWidth=${metrics.scrollWidth} > innerWidth=${metrics.innerWidth}`);
    } else {
      log(`[${label}] 无横向溢出`);
    }
  } catch (err) { warn(`[${label}] 溢出检测失败：${err.message}`); }
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

async function login(page, username, password) {
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
  await sleep(900);
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
    if (resp.url().includes('/api/') && resp.status() >= 500) {
      failedRequests.push({ page: label, url: resp.url(), status: resp.status() });
    }
  });
}

async function ensureMember(page) {
  const created = await api(page, 'POST', '/api/users', {
    username: MEMBER_USER, password: MEMBER_PASS, display_name: 'QA 成员', role: 'member',
  });
  if (created.status === 201) return created.json;
  if (created.status === 409) {
    const listed = await api(page, 'GET', '/api/users?query=' + encodeURIComponent(MEMBER_USER));
    const user = (listed.json?.items || []).find((u) => u.username === MEMBER_USER);
    if (user) {
      await api(page, 'POST', `/api/users/${user.id}/reset-password`, { new_password: MEMBER_PASS });
      await api(page, 'PATCH', `/api/users/${user.id}`, { disabled: false });
      return user;
    }
  }
  throw new Error(`确保成员账号失败：HTTP ${created.status} ${JSON.stringify(created.json)}`);
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  log(`目标服务：${BASE_URL}`);
  log(`输出目录：${OUT_DIR}`);
  let browser;
  let exitCode = 0;
  const contextList = [];
  try {
    browser = await chromium.launch({
      headless: true,
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--font-render-hinting=none'],
    });
    const newContext = async () => {
      const ctx = await browser.newContext({
        viewport: VIEWPORT, deviceScaleFactor: 1, locale: 'zh-CN',
        timezoneId: 'Asia/Shanghai', colorScheme: 'light',
      });
      contextList.push(ctx);
      return ctx;
    };

    // ===================== 管理员上下文 =====================
    const adminCtx = await newContext();
    const adminPage = await adminCtx.newPage();
    attachDiagnostics(adminPage, 'admin');
    await login(adminPage, ADMIN_USER, ADMIN_PASS);
    ok('管理员登录成功');

    const member = await ensureMember(adminPage);
    ok(`成员账号就绪：${member.username} (id=${member.id})`);

    const recList = await api(adminPage, 'GET', '/api/recordings?query=TraceOpt&page_size=10');
    const demo = (recList.json?.items || [])[0];
    if (!demo) throw new Error('找不到演示录音 TraceOpt，无法创建分享');
    log(`演示录音 id=${demo.id}`);

    // 通过 API 预置三种分享（开放 / 密码 / 即将过期），供截图与公开页使用
    const openShare = await api(adminPage, 'POST', `/api/recordings/${demo.id}/shares`, {
      include_transcript: true, include_insights: true, allow_download: false,
    });
    if (openShare.status !== 201) throw new Error(`创建开放分享失败：${openShare.status} ${JSON.stringify(openShare.json)}`);
    const pwShare = await api(adminPage, 'POST', `/api/recordings/${demo.id}/shares`, { password: 'qa-pass-123' });
    const expShare = await api(adminPage, 'POST', `/api/recordings/${demo.id}/shares`, {
      expires_at: new Date(Date.now() + 6000).toISOString(),
    });
    log(`分享 token：open=${openShare.json.token.slice(0, 8)}… pw=${pwShare.json?.token?.slice(0, 8)}… exp=${expShare.json?.token?.slice(0, 8)}…`);

    // ---------------- 01 用户管理 ----------------
    log('—— 01 用户管理 ——');
    try {
      await adminPage.goto(`${BASE_URL}/users`, { waitUntil: 'domcontentloaded' });
      await adminPage.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(adminPage, [/用户管理/, /用户名/], 15000);
      await sleep(1200);
      await shot(adminPage, '01-users.png', '用户管理：表格 + 角色 + 操作');
      await checkOverflow(adminPage, 'users');
      // 打开创建用户对话框再关闭（验证交互）
      try {
        await clickByText(adminPage, [/新建用户/, /创建用户/, /新增用户/]);
        await sleep(800);
        await shot(adminPage, '01b-user-create-dialog.png', '用户管理：创建用户对话框');
        await adminPage.keyboard.press('Escape');
        await sleep(400);
      } catch (err) { warn(`创建用户对话框未覆盖：${err.message}`); }
    } catch (err) {
      warn(`用户管理页异常：${err.message}`);
      await shot(adminPage, '01-users.png', '用户管理（异常状态下截图）');
    }

    // ---------------- 02 详情页分享弹窗 ----------------
    log('—— 02 分享弹窗 ——');
    try {
      await adminPage.goto(`${BASE_URL}/r/${demo.id}`, { waitUntil: 'domcontentloaded' });
      await adminPage.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(adminPage, [/AI问答/, /AI 问答/, /转写/, /观点树/], 20000);
      await sleep(1200);
      await clickByText(adminPage, [/分享/]);
      await sleep(1500);
      await waitForAnyText(adminPage, [/分享链接/, /创建分享/, /已有分享/, /链接/], 15000);
      await shot(adminPage, '02-share-dialog.png', '详情页：分享弹窗（创建链接 + 已有分享列表）');
      await checkOverflow(adminPage, 'share-dialog');
      await adminPage.keyboard.press('Escape').catch(() => {});
      await sleep(400);
    } catch (err) {
      warn(`分享弹窗异常：${err.message}`);
      await shot(adminPage, '02-share-dialog.png', '分享弹窗（异常状态下截图）');
    }

    // ---------------- 03/04/07 公开分享页（匿名上下文） ----------------
    const anonCtx = await newContext();
    const anonPage = await anonCtx.newPage();
    attachDiagnostics(anonPage, 'public');

    log('—— 03 公开分享页（未登录） ——');
    try {
      await anonPage.goto(`${BASE_URL}/s/${openShare.json.token}`, { waitUntil: 'domcontentloaded' });
      await anonPage.waitForLoadState('networkidle').catch(() => {});
      await sleep(2000);
      if (anonPage.url().includes('/login')) throw new Error('公开分享页被重定向到 /login（不允许）');
      await waitForAnyText(anonPage, [/TraceOpt/, /转写/, /AI纪要/, /AI 纪要/, /观点树/], 20000);
      await shot(anonPage, '03-public-share.png', '公开分享页（未登录：播放器 + 只读页签）');
      await checkOverflow(anonPage, 'public-share');
      const bodyText = await anonPage.locator('body').innerText();
      if (!/Echoic/.test(bodyText)) warn('公开页未检测到品牌/页脚文字');
    } catch (err) {
      warn(`公开分享页异常：${err.message}`);
      await shot(anonPage, '03-public-share.png', '公开分享页（异常状态下截图）');
    }

    log('—— 04 公开分享 · 密码页 ——');
    try {
      await anonPage.goto(`${BASE_URL}/s/${pwShare.json.token}`, { waitUntil: 'domcontentloaded' });
      await anonPage.waitForLoadState('networkidle').catch(() => {});
      await sleep(1500);
      if (anonPage.url().includes('/login')) throw new Error('密码分享页被重定向到 /login');
      await waitForAnyText(anonPage, [/密码/, /解锁/, /受保护/], 15000);
      await shot(anonPage, '04-public-password.png', '公开分享：密码解锁页');
      await checkOverflow(anonPage, 'public-password');
      // UI 解锁验证（正确密码后应出现内容）
      const pwInput = anonPage.locator('input[type="password"]').first();
      if ((await pwInput.count()) > 0) {
        await pwInput.fill('qa-pass-123');
        await clickByText(anonPage, [/解锁/, /进入/, /查看/]).catch(() => {});
        await sleep(2200);
        const url2 = anonPage.url();
        if (url2.includes('/login')) throw new Error('解锁后跳转 /login（不允许）');
        log(`密码解锁后 URL=${url2}`);
      }
    } catch (err) {
      warn(`密码分享页异常：${err.message}`);
      await shot(anonPage, '04-public-password.png', '密码分享页（异常状态下截图）');
    }

    // ---------------- 05 设置 · 系统（consume/注册） ----------------
    log('—— 05 设置 · 系统 tab ——');
    try {
      await adminPage.goto(`${BASE_URL}/settings?tab=system`, { waitUntil: 'domcontentloaded' });
      await adminPage.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(adminPage, [/系统/, /自动导入/, /consume/i], 15000);
      await sleep(1200);
      // 点击「立即扫描」触发一次扫描并展示结果
      try {
        await clickByText(adminPage, [/立即扫描/, /扫描/]);
        await sleep(2500);
      } catch (err) { warn(`立即扫描按钮未找到：${err.message}`); }
      await shot(adminPage, '05-consume-settings.png', '设置 · 系统：开放注册 + consume 自动导入（含扫描结果）');
      await checkOverflow(adminPage, 'settings-system');
      const text = await adminPage.locator('body').innerText();
      if (!/注册/.test(text)) warn('设置系统 tab 未检测到注册开关文案');
      if (!/导入|consume/i.test(text)) warn('设置系统 tab 未检测到 consume 文案');
    } catch (err) {
      warn(`设置系统 tab 异常：${err.message}`);
      await shot(adminPage, '05-consume-settings.png', '设置系统 tab（异常状态下截图）');
    }

    // ---------------- 06 成员视角资料库 ----------------
    log('—— 06 成员视角资料库 ——');
    const memberCtx = await newContext();
    const memberPage = await memberCtx.newPage();
    attachDiagnostics(memberPage, 'member');
    try {
      await login(memberPage, MEMBER_USER, MEMBER_PASS);
      await memberPage.goto(`${BASE_URL}/`, { waitUntil: 'domcontentloaded' });
      await memberPage.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(memberPage, [/我的录音库/, /录音库/, /还没有你自己的录音/], 15000);
      await sleep(1200);
      await shot(memberPage, '06-member-library.png', '成员视角资料库（无管理入口；仅自己的录音）');
      await checkOverflow(memberPage, 'member-library');
      const memberText = await memberPage.locator('body').innerText();
      if (/用户管理/.test(memberText)) warn('成员资料库出现了「用户管理」入口（应隐藏）');
      // 成员直接访问 /users 应被重定向并提示
      await memberPage.goto(`${BASE_URL}/users`, { waitUntil: 'domcontentloaded' });
      await sleep(1200);
      const redirected = !memberPage.url().includes('/users');
      log(`成员访问 /users → URL=${memberPage.url()}（redirect=${redirected}）`);
      if (!redirected) warn('成员直接访问 /users 未被重定向（权限守卫缺失？）');
    } catch (err) {
      warn(`成员视角异常：${err.message}`);
      await shot(memberPage, '06-member-library.png', '成员资料库（异常状态下截图）');
    }

    // ---------------- 07 公开分享 · 过期/失效 ----------------
    log('—— 07 公开分享 · 过期提示 ——');
    try {
      await sleep(2500); // 等待 6s 有效期分享过期
      await anonPage.goto(`${BASE_URL}/s/${expShare.json.token}`, { waitUntil: 'domcontentloaded' });
      await anonPage.waitForLoadState('networkidle').catch(() => {});
      await sleep(1800);
      if (anonPage.url().includes('/login')) throw new Error('过期分享页被重定向到 /login');
      const text = await anonPage.locator('body').innerText();
      await shot(anonPage, '07-public-expired.png', '公开分享：过期/失效提示页');
      await checkOverflow(anonPage, 'public-expired');
      if (!/过期|失效|不存在|撤销/.test(text)) warn(`过期页未检测到友好提示文案：${text.slice(0, 120)}`);
    } catch (err) {
      warn(`过期分享页异常：${err.message}`);
      await shot(anonPage, '07-public-expired.png', '过期分享页（异常状态下截图）');
    }

    // ===================== 诊断汇总 =====================
    // 预期噪声：登录前 /api/auth/me 401；密码分享未解锁时 public 接口 401
    const expectedConsole = consoleErrors.filter((e) => /401|403/.test(e.text) || /Failed to load resource/.test(e.text));
    const unexpectedConsole = consoleErrors.filter((e) => !expectedConsole.includes(e));
    console.log('\n================ v0.2 视觉巡检汇总 ================');
    console.log(`截图数量：${savedShots.length}`);
    for (const item of savedShots) console.log(`  ${item.filename}  ${item.note}`);
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
      resolve(OUT_DIR, '..', '..', '..', 'e2e', 'screenshot-report-v02.json'),
      JSON.stringify({ baseUrl: BASE_URL, viewport: VIEWPORT, savedShots, consoleErrors, pageErrors, failedRequests, overflowIssues, warnings }, null, 2),
      'utf-8',
    );

    const required = ['01-users.png', '02-share-dialog.png', '03-public-share.png', '04-public-password.png', '05-consume-settings.png', '06-member-library.png', '07-public-expired.png'];
    const missing = required.filter((name) => !savedShots.some((s) => s.filename === name));
    if (missing.length) { console.error(`\x1b[1;31m[FAIL]\x1b[0m 关键截图缺失：${missing.join(', ')}`); exitCode = 1; }
    if (pageErrors.length > 0) warn(`存在 ${pageErrors.length} 条未捕获页面异常（详见 e2e/screenshot-report-v02.json）`);
    if (unexpectedConsole.length > 0) warn(`存在 ${unexpectedConsole.length} 条非预期控制台错误`);
    console.log(exitCode === 0 ? '\x1b[1;32mv0.2 截图任务：PASS ✔\x1b[0m' : '\x1b[1;31mv0.2 截图任务：FAIL\x1b[0m');
  } catch (err) {
    console.error(`\x1b[1;31m[FATAL]\x1b[0m ${err.stack || err}`);
    exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  process.exit(exitCode);
}

main();
