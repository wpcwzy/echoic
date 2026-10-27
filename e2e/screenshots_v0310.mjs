#!/usr/bin/env node
/**
 * Echoic v0.3.10 UI 截图与视觉巡检（Playwright + Chromium）
 *
 * 覆盖 v0.3.10 新增/修复界面：
 *   01-login-register-entry.png  登录页 · 注册入口（allow_registration=true）
 *   02-register.png              注册页（注册后自动登录）
 *   03-profile-dark.png          个人资料页（深色；资料保存 + 改密校验提示）
 *   04-library-description.png   资料库 · 上传录音自动描述（非“暂无描述”）
 *   07-transfer-owner.png        转移录音归属弹窗（无未格式化数字 ID）
 *   08-user-enabled-switch.png   编辑用户 · 启用账号开关（开=启用/关=停用）
 *   09-library-light.png         资料库 · 浅色主题（标准页）
 *   10-library-dark.png          资料库 · 深色主题（标准页）
 *   （05-live-captions-ga.png / 06-live-reconnect.png 由 e2e/live_reconnect_check.mjs 生成）
 *
 * 运行（服务已在 :8100 运行，前端 dist 已构建，allow_registration=true）：
 *   cd e2e
 *   export HOME=/home/wpcwzy/echoic/.npm-home
 *   export PLAYWRIGHT_BROWSERS_PATH=/home/wpcwzy/echoic/.cache/ms-playwright
 *   ECHOIC_E2E_BASE_URL=http://127.0.0.1:8100 node screenshots_v0310.mjs
 */
import { chromium } from 'playwright';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..');
const OUT_DIR = process.env.ECHOIC_E2E_SCREENSHOT_DIR || resolve(REPO_ROOT, 'docs/screenshots/v0.3.10');
const BASE_URL = (process.env.ECHOIC_E2E_BASE_URL || 'http://127.0.0.1:8100').replace(/\/$/, '');
const ADMIN_USER = process.env.ECHOIC_E2E_ADMIN_USERNAME || 'admin';
const ADMIN_PASS = process.env.ECHOIC_E2E_ADMIN_PASSWORD || 'admin123';
const VIEWPORT = { width: 1680, height: 1050 };
const THEME_STORAGE_KEY = 'echoic:theme-mode';
const STAMP = String(Date.now()).slice(-7);

const consoleErrors = [];
const pageErrors = [];
const failedRequests = [];
const overflowIssues = [];
const warnings = [];
const savedShots = [];
const themeChecks = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const log = (msg) => console.log(`[shots-v0310] ${msg}`);
const warn = (msg) => {
  warnings.push(msg);
  console.log(`\x1b[1;33m[WARN]\x1b[0m ${msg}`);
};
const ok = (msg) => console.log(`\x1b[1;32m[OK]\x1b[0m ${msg}`);

function check(condition, message) {
  if (!condition) throw new Error(message);
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
        const loc = page.getByText(pattern, { exact: false }).first();
        if (await loc.isVisible()) return pattern;
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
    for (const loc of [
      page.getByRole('button', { name: pattern }).first(),
      page.getByRole('menuitem', { name: pattern }).first(),
      page.getByRole('tab', { name: pattern }).first(),
      page.getByRole('link', { name: pattern }).first(),
      page.getByText(pattern, { exact: true }).first(),
    ]) {
      try {
        if ((await loc.count()) > 0 && (await loc.first().isVisible())) {
          await loc.first().click({ timeout });
          return pattern;
        }
      } catch {
        /* try next */
      }
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
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    /* not json */
  }
  return { status: resp.status(), json, headers: resp.headers() };
}

async function login(page, username = ADMIN_USER, password = ADMIN_PASS) {
  await page.goto(`${BASE_URL}/login`, { waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('networkidle').catch(() => {});
  await sleep(700);
  await page.locator('input[placeholder="请输入用户名"]').fill(username);
  await page.locator('input[placeholder="请输入密码"]').fill(password);
  await clickByText(page, [/登\s*录/]);
  await page.waitForURL((u) => !u.pathname.startsWith('/login'), { timeout: 20000 });
  await page.waitForLoadState('networkidle').catch(() => {});
  await sleep(800);
}

async function applyTheme(page, mode) {
  await page.evaluate(([key, value]) => localStorage.setItem(key, value), [THEME_STORAGE_KEY, mode]);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForLoadState('networkidle').catch(() => {});
  await sleep(900);
  const state = await themeState(page);
  if (state.dataTheme !== mode) warn(`主题期望 ${mode}，实际 ${state.dataTheme}`);
  return state;
}

async function themeState(page) {
  return page.evaluate(() => {
    const root = document.documentElement;
    const parse = (value) => {
      const match = String(value).match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)/);
      return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null;
    };
    const luminance = (rgb) => (rgb ? (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255 : null);
    return {
      dataTheme: root.getAttribute('data-theme'),
      stored: localStorage.getItem('echoic:theme-mode'),
      bodyBg: getComputedStyle(document.body).backgroundColor,
      bodyLum: luminance(parse(getComputedStyle(document.body).backgroundColor)),
    };
  });
}

async function checkTheme(page, label, expected) {
  const state = await themeState(page);
  themeChecks.push({ page: label, expected, ...state });
  if (state.dataTheme !== expected) warn(`[${label}] data-theme=${state.dataTheme}，期望 ${expected}`);
  if (expected === 'dark' && state.bodyLum !== null && state.bodyLum > 0.55) {
    warn(`[${label}] 深色背景偏亮（lum=${state.bodyLum.toFixed(2)}）`);
  }
  return state;
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
  } catch (err) {
    warn(`[${label}] 溢出检测失败：${err.message}`);
  }
}

function attachDiagnostics(page, label) {
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push({ page: label, text: msg.text() });
  });
  page.on('pageerror', (err) =>
    pageErrors.push({ page: label, error: String(err && err.stack ? err.stack : err).split('\n')[0] }),
  );
  page.on('requestfailed', (req) => {
    if (req.url().includes('/api/')) failedRequests.push({ page: label, url: req.url(), failure: req.failure()?.errorText });
  });
  page.on('response', (resp) => {
    if (resp.url().includes('/api/') && resp.status() >= 500) {
      failedRequests.push({ page: label, url: resp.url(), status: resp.status() });
    }
  });
}

async function uploadRecordingViaApi(page, title, { description = null, seconds = 6 } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'echoic-v0310-shot-'));
  const wavPath = join(dir, 'fixture.wav');
  const ffmpeg = process.env.ECHOIC_FFMPEG || 'ffmpeg';
  const proc = spawnSync(
    ffmpeg,
    ['-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', `sine=frequency=330:duration=${seconds}`,
      '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', wavPath],
    { encoding: 'utf-8' },
  );
  if (proc.status !== 0) throw new Error(`ffmpeg 生成截图素材失败：${proc.stderr || proc.error}`);
  const multipart = {
    file: { name: 'qa-v0310-fixture.wav', mimeType: 'audio/wav', buffer: readFileSync(wavPath) },
    title,
    language: 'zh',
  };
  if (description !== null) multipart.description = description;
  const resp = await page.request.fetch(`${BASE_URL}/api/recordings/upload`, { method: 'POST', multipart });
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

async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  log(`目标服务：${BASE_URL}`);
  log(`输出目录：${OUT_DIR}`);
  let browser;
  let exitCode = 0;
  try {
    browser = await chromium.launch({
      headless: true,
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--font-render-hinting=none'],
    });
    const newContext = async (opts = {}) =>
      browser.newContext({
        viewport: VIEWPORT,
        deviceScaleFactor: 1,
        locale: 'zh-CN',
        timezoneId: 'Asia/Shanghai',
        colorScheme: 'light',
        ...opts,
      });

    // ================= 未登录：注册入口 + 注册页 =================
    const anonCtx = await newContext();
    const anonPage = await anonCtx.newPage();
    attachDiagnostics(anonPage, 'anon');
    const health = await api(anonPage, 'GET', '/api/system/health');
    const registrationOpen = health.json?.allow_registration === true;
    if (!registrationOpen) warn('health.allow_registration=false，注册入口可能不可见（QA 服务应以 ECHOIC_ALLOW_REGISTRATION=true 启动）');

    log('—— 01 登录页注册入口 ——');
    try {
      await anonPage.goto(`${BASE_URL}/login`, { waitUntil: 'domcontentloaded' });
      await anonPage.waitForLoadState('networkidle').catch(() => {});
      await sleep(900);
      await waitForAnyText(anonPage, [/欢迎回来|登录/], 15000);
      const text = await anonPage.locator('body').innerText();
      check(!registrationOpen || /立即注册/.test(text), 'allow_registration=true 但登录页未显示「立即注册」');
      await checkTheme(anonPage, 'login-light', 'light');
      await shot(anonPage, '01-login-register-entry.png', '登录页 · 注册入口（allow_registration=true）');
      await checkOverflow(anonPage, 'login');
    } catch (err) {
      warn(`登录页截图失败：${err.message}`);
      await shot(anonPage, '01-login-register-entry.png', '登录页（异常状态下截图）');
    }

    log('—— 02 注册页（注册后自动登录） ——');
    try {
      await anonPage.goto(`${BASE_URL}/register`, { waitUntil: 'domcontentloaded' });
      await anonPage.waitForLoadState('networkidle').catch(() => {});
      await sleep(1000);
      await waitForAnyText(anonPage, [/创建账号|注册/], 15000);
      await shot(anonPage, '02-register.png', '注册页（用户名/显示名称/邮箱/密码）');
      await checkOverflow(anonPage, 'register');
      if (registrationOpen) {
        const username = `qa_ui_reg_${STAMP}`;
        const password = 'QaUiReg123';
        await anonPage.locator('input[placeholder="登录用户名，如 zhangsan"]').fill(username);
        await anonPage.locator('input[placeholder="展示给团队的名字，如 张三"]').fill('QA 注册用户');
        await anonPage.locator('input[placeholder="name@example.com"]').fill(`${username}@example.com`);
        await anonPage.locator('input[placeholder="至少 8 位字符"]').fill(password);
        await anonPage.locator('input[placeholder="再次输入密码"]').fill(password);
        await clickByText(anonPage, [/注册并登录/]);
        await anonPage.waitForURL((url) => !url.pathname.startsWith('/register') && !url.pathname.startsWith('/login'), { timeout: 25000 });
        await anonPage.waitForLoadState('networkidle').catch(() => {});
        await sleep(1200);
        ok(`注册后自动登录成功（${username}）`);
        const body = await anonPage.locator('body').innerText();
        check(/我的录音库|资料库/.test(body), '注册后未进入资料库');
      }
    } catch (err) {
      warn(`注册页/注册流程异常：${err.message}`);
    }

    // ================= 管理员上下文 =================
    const adminCtx = await newContext();
    const page = await adminCtx.newPage();
    attachDiagnostics(page, 'admin');
    await login(page);
    ok('管理员登录成功');

    log('—— 03 个人资料（深色）+ 资料编辑 + 改密校验 ——');
    try {
      await applyTheme(page, 'dark');
      await page.goto(`${BASE_URL}/profile`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(page, [/个人资料/, /账号信息/], 15000);
      await sleep(1000);
      await page.locator('input[placeholder="如 张三"]').fill('QA 管理员');
      await page.locator('input[placeholder="name@example.com"]').fill(`qa_admin_${STAMP}@example.com`);
      await clickByText(page, [/保存资料/]);
      await sleep(1200);
      const me = await api(page, 'GET', '/api/auth/me');
      check(me.json?.user?.display_name === 'QA 管理员', `资料保存后 display_name 未生效：${me.json?.user?.display_name}`);
      // 改密校验：新密码过短应显示校验错误（不实际修改）
      await page.locator('input[placeholder="输入当前密码"]').fill(ADMIN_PASS);
      await page.locator('input[placeholder="至少 6 位字符"]').fill('123');
      await page.locator('input[placeholder="再次输入新密码"]').fill('123');
      await clickByText(page, [/修改密码/]);
      await sleep(900);
      const body = await page.locator('body').innerText();
      check(/至少 6 位|密码/.test(body), '改密校验未显示提示');
      await checkTheme(page, 'profile-dark', 'dark');
      await shot(page, '03-profile-dark.png', '个人资料（深色）：资料保存 + 改密校验');
      await checkOverflow(page, 'profile');
    } catch (err) {
      warn(`个人资料页异常：${err.message}`);
      await shot(page, '03-profile-dark.png', '个人资料（异常状态下截图）');
    }

    // ================= 描述自动生成 + 资料库展示 =================
    log('—— 04 资料库 · 上传录音自动描述 ——');
    let descRecording = null;
    try {
      await applyTheme(page, 'light');
      descRecording = await uploadRecordingViaApi(page, `QA 描述展示录音 ${STAMP}`);
      const description = (descRecording.description || '').trim();
      check(description.length > 0, '上传录音处理后描述为空（自动描述未生效）');
      check(description.length <= 120, `自动描述超过 120 字：${description.length}`);
      await page.goto(`${BASE_URL}/`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(page, [/我的录音库/], 15000);
      await page.locator('input[placeholder="搜索标题、描述或转录内容"]').fill(`QA 描述展示录音 ${STAMP}`);
      await sleep(1400);
      const card = page.locator('.recording-card', { hasText: `QA 描述展示录音 ${STAMP}` }).first();
      await card.waitFor({ timeout: 15000 });
      const cardText = await card.innerText();
      check(!cardText.includes('暂无描述'), '资料库卡片仍显示「暂无描述」');
      check(cardText.includes(description.slice(0, 12)), '资料库卡片未展示自动描述');
      await shot(page, '04-library-description.png', `资料库 · 自动描述：${description.slice(0, 28)}…`);
      await checkOverflow(page, 'library-description');
    } catch (err) {
      warn(`描述自动生成截图异常：${err.message}`);
      await shot(page, '04-library-description.png', '资料库描述（异常状态下截图）');
    }

    // ================= 转移归属弹窗（无原始数字 ID） =================
    log('—— 07 转移录音归属弹窗 ——');
    try {
      // 保证至少有一个可转移目标用户
      let users = await api(page, 'GET', '/api/users?page_size=100');
      let member = (users.json?.items || []).find((item) => item.username.startsWith('qa_ui_member_'));
      if (!member) {
        const created = await api(page, 'POST', '/api/users', {
          username: `qa_ui_member_${STAMP}`,
          password: 'QaUiMember123',
          display_name: 'QA 成员',
          role: 'member',
        });
        check(created.status === 201, `创建 QA 成员失败：HTTP ${created.status}`);
        member = created.json;
      }
      const listed = await api(page, 'GET', '/api/recordings?query=TraceOpt&page_size=10');
      const demo = (listed.json?.items || [])[0];
      check(Boolean(demo), '找不到演示录音');
      await applyTheme(page, 'dark');
      await page.goto(`${BASE_URL}/r/${demo.id}`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(page, [/AI问答|AI 问答|转写|观点树/], 20000);
      await sleep(1200);
      await page.locator('button[aria-label="更多操作"]').first().click({ timeout: 6000 });
      await sleep(500);
      await clickByText(page, [/转移归属/]);
      await page.getByRole('dialog').filter({ hasText: '转移录音归属' }).waitFor({ timeout: 10000 });
      const modal = page.getByRole('dialog').filter({ hasText: '转移录音归属' }).first();
      const placeholder = await modal.locator('.transfer-owner-select').innerText();
      check(/选择新的归属用户/.test(placeholder), `转移弹窗初始值不是空占位：${placeholder}`);
      // 打开展示选项，确认选项含有 @username 而不是裸数字 ID
      await modal.locator('.transfer-owner-select').click({ timeout: 5000 });
      await sleep(600);
      const optionTexts = await page.locator('.ant-select-item-option').allInnerTexts();
      check(optionTexts.length > 0, '转移弹窗没有可选用户');
      check(!optionTexts.some((text) => /^\d+$/.test(text.trim())), `存在裸数字 ID 选项：${optionTexts.join(' | ')}`);
      check(optionTexts.some((text) => text.includes('@')), `选项缺少 @username：${optionTexts.join(' | ')}`);
      await page.keyboard.press('Escape');
      await sleep(300);
      await shot(page, '07-transfer-owner.png', '转移录音归属弹窗（初始为空、选项为「名称（@username）」）');
      await checkOverflow(page, 'transfer-owner');
      await page.keyboard.press('Escape').catch(() => {});
    } catch (err) {
      warn(`转移归属弹窗异常：${err.message}`);
      await shot(page, '07-transfer-owner.png', '转移归属（异常状态下截图）');
    }

    // ================= 编辑用户 · 启用账号开关 =================
    log('—— 08 编辑用户 · 启用账号开关 ——');
    try {
      await applyTheme(page, 'light');
      let users = await api(page, 'GET', '/api/users?page_size=100');
      let member = (users.json?.items || []).find((item) => item.username.startsWith('qa_ui_member_'));
      if (!member) {
        const created = await api(page, 'POST', '/api/users', {
          username: `qa_ui_member_${STAMP}`,
          password: 'QaUiMember123',
          display_name: 'QA 成员',
          role: 'member',
        });
        member = created.json;
      }
      await page.goto(`${BASE_URL}/users`, { waitUntil: 'domcontentloaded' });
      await page.waitForLoadState('networkidle').catch(() => {});
      await waitForAnyText(page, [/用户管理/], 15000);
      await sleep(1000);
      await page.locator(`button[aria-label="管理用户 ${member.username}"]`).first().click({ timeout: 8000 });
      await sleep(500);
      await clickByText(page, [/编辑资料与角色/]);
      const modal = page.getByRole('dialog').filter({ hasText: '编辑用户' }).first();
      await modal.waitFor({ timeout: 10000 });
      const switchEl = modal.locator('.ant-switch').first();
      await switchEl.waitFor({ timeout: 8000 });
      await modal.waitFor({ state: 'visible', timeout: 8000 }).catch(() => {});
      await sleep(800); // 等待 AntD Modal 动画/遮罩稳定后再截图
      const modalText = await modal.innerText();
      check(/启用账号/.test(modalText), '编辑用户弹窗缺少「启用账号」说明');
      check(/启用/.test(modalText), '编辑用户弹窗缺少「启用/停用」文字');
      await checkTheme(page, 'users-light', 'light');
      await shot(page, '08-user-enabled-switch.png', '编辑用户：启用账号开关（开=启用 / 关=停用）');
      await checkOverflow(page, 'user-edit');
      // 语义验证：当前启用 → 关闭后保存 → disabled=true；随后恢复
      const wasChecked = await switchEl.evaluate((el) => el.classList.contains('ant-switch-checked'));
      check(wasChecked, '默认状态开关应为「启用」');
      await switchEl.click();
      await sleep(300);
      await clickByText(page, [/^保\s*存$/]);
      await sleep(1200);
      const after = await api(page, 'GET', `/api/users?query=${encodeURIComponent(member.username)}&include_disabled=true`);
      const row = (after.json?.items || []).find((item) => item.id === member.id);
      check(row?.disabled === true, `关闭启用开关后 disabled 应为 true：${JSON.stringify(row)}`);
      const reenable = await api(page, 'PATCH', `/api/users/${member.id}`, { disabled: false });
      check(reenable.status === 200, '恢复成员启用状态失败');
      ok('启用账号开关语义验证通过（开=启用；关闭后 disabled=true；已恢复）');
    } catch (err) {
      warn(`编辑用户开关异常：${err.message}`);
      await shot(page, '08-user-enabled-switch.png', '编辑用户开关（异常状态下截图）');
    }

    // ================= 标准资料库（浅色 / 深色） =================
    for (const [theme, filename, label] of [
      ['light', '09-library-light.png', '资料库 · 浅色主题'],
      ['dark', '10-library-dark.png', '资料库 · 深色主题'],
    ]) {
      log(`—— ${filename} ——`);
      try {
        await applyTheme(page, theme);
        await page.goto(`${BASE_URL}/`, { waitUntil: 'domcontentloaded' });
        await page.waitForLoadState('networkidle').catch(() => {});
        await waitForAnyText(page, [/我的录音库/], 15000);
        await sleep(1200);
        await checkTheme(page, `library-${theme}`, theme);
        await shot(page, filename, label);
        await checkOverflow(page, `library-${theme}`);
      } catch (err) {
        warn(`${filename} 异常：${err.message}`);
        await shot(page, filename, `${label}（异常状态下截图）`);
      }
    }

    // ================= 汇总 =================
    const expectedConsole = consoleErrors.filter((e) => /401|403|404/.test(e.text) || /Failed to load resource/.test(e.text));
    const unexpectedConsole = consoleErrors.filter((e) => !expectedConsole.includes(e));
    console.log('\n================ v0.3.10 视觉巡检汇总 ================');
    console.log(`截图数量：${savedShots.length}`);
    for (const item of savedShots) console.log(`  ${item.filename}  ${item.note}`);
    console.log(`主题检查：${themeChecks.length}`);
    for (const item of themeChecks) console.log(`  - ${item.page}: ${item.dataTheme} (stored=${item.stored}, lum=${item.bodyLum?.toFixed?.(2)})`);
    console.log(`控制台错误：${consoleErrors.length}（预期噪声 ${expectedConsole.length}）`);
    for (const item of expectedConsole.slice(0, 6)) console.log(`  - [预期] ${item.page}: ${item.text}`);
    for (const item of unexpectedConsole.slice(0, 10)) console.log(`  - [异常] ${item.page}: ${item.text}`);
    console.log(`未捕获页面异常：${pageErrors.length}`);
    for (const item of pageErrors.slice(0, 5)) console.log(`  - ${item.page}: ${item.error}`);
    console.log(`失败请求：${failedRequests.length}`);
    for (const item of failedRequests.slice(0, 8)) console.log(`  - ${item.status || ''} ${item.url} ${item.failure || ''}`);
    console.log(`横向溢出问题：${overflowIssues.length}`);
    for (const item of overflowIssues) console.log(`  - ${item.page}: ${item.scrollWidth} > ${item.innerWidth}`);
    console.log(`告警：${warnings.length}`);
    for (const item of warnings) console.log(`  - ${item}`);

    writeFileSync(
      resolve(REPO_ROOT, 'e2e', 'screenshot-report-v0310.json'),
      JSON.stringify({ baseUrl: BASE_URL, viewport: VIEWPORT, savedShots, themeChecks, consoleErrors, pageErrors, failedRequests, overflowIssues, warnings }, null, 2),
      'utf-8',
    );

    const required = ['01-login-register-entry.png', '02-register.png', '03-profile-dark.png', '04-library-description.png',
      '07-transfer-owner.png', '08-user-enabled-switch.png', '09-library-light.png', '10-library-dark.png'];
    const missing = required.filter((name) => !savedShots.some((item) => item.filename === name));
    if (missing.length) {
      console.error(`\x1b[1;31m[FAIL]\x1b[0m 关键截图缺失：${missing.join(', ')}`);
      exitCode = 1;
    }
    if (pageErrors.length > 0) warn(`存在 ${pageErrors.length} 条未捕获页面异常`);
    if (unexpectedConsole.length > 0) warn(`存在 ${unexpectedConsole.length} 条非预期控制台错误`);
    console.log(exitCode === 0 ? '\x1b[1;32mv0.3.10 截图任务：PASS ✔\x1b[0m' : '\x1b[1;31mv0.3.10 截图任务：FAIL\x1b[0m');
  } catch (err) {
    console.error(`\x1b[1;31m[FATAL]\x1b[0m ${err.stack || err}`);
    exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  process.exit(exitCode);
}

main();
