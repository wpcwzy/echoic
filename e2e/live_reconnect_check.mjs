#!/usr/bin/env node
/**
 * Echoic v0.3.10 实时字幕 GA · 断线重连（Real E2E）
 *
 * 真实链路：浏览器 → 本地 TCP 代理 → FastAPI（mock live provider）→ 前端。代理主动掐断
 * WS TCP 连接模拟网络中断，验证 120s 内自动重连/续传：
 *   - 页面无 "Beta" 标识；实时字幕连接成功（mock provider）
 *   - 断线后 UI 显示「重连中…」，自动以 1s/2s/4s 退避重连
 *   - 重连 ready 事件 resumed=true、base_ms>0、connection 递增（时间戳续传）
 *   - 首次重连失败后下一次退避重试可恢复；字幕继续增长
 *   - 正常停止后不再重连；片段连续、无重复
 *
 * 运行（服务已在 :8100 运行，前端 dist 已构建）：
 *   cd e2e
 *   export HOME=/home/wpcwzy/echoic/.npm-home
 *   export PLAYWRIGHT_BROWSERS_PATH=/home/wpcwzy/echoic/.cache/ms-playwright
 *   ECHOIC_E2E_UPSTREAM_PORT=8100 node live_reconnect_check.mjs
 *
 * 产物：
 *   docs/screenshots/v0.3.10/05-live-captions-ga.png   （连接中、有字幕、无 Beta）
 *   docs/screenshots/v0.3.10/06-live-reconnect.png     （重连中状态）
 *   e2e/live-reconnect-report-v0310.json               （结果 JSON）
 */
import { chromium } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createLiveProxy } from './live_proxy.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..');
const UPSTREAM_PORT = Number(process.env.ECHOIC_E2E_UPSTREAM_PORT || 8100);
const OUT_DIR = process.env.ECHOIC_E2E_SCREENSHOT_DIR || resolve(REPO_ROOT, 'docs/screenshots/v0.3.10');
const REPORT_PATH = resolve(REPO_ROOT, 'e2e', 'live-reconnect-report-v0310.json');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok: Boolean(ok), detail });
  console.log(`${ok ? '\x1b[1;32mPASS\x1b[0m' : '\x1b[1;31mFAIL\x1b[0m'}  ${name}${detail ? `  [${detail}]` : ''}`);
}

async function shot(page, filename, note) {
  mkdirSync(OUT_DIR, { recursive: true });
  const path = resolve(OUT_DIR, filename);
  await page.screenshot({ path, animations: 'disabled' });
  console.log(`\x1b[1;32m[OK]\x1b[0m 截图 ${filename}（${note}）`);
  return path;
}

async function main() {
  const proxy = await createLiveProxy({ upstreamPort: UPSTREAM_PORT });
  const base = proxy.baseUrl;
  console.log(`[live-reconnect] upstream=:${UPSTREAM_PORT} proxy=${base}`);

  const browser = await chromium.launch({
    headless: true,
    args: [
      '--no-sandbox',
      '--disable-dev-shm-usage',
      '--use-fake-ui-for-media-stream',
      '--use-fake-device-for-media-stream',
      '--autoplay-policy=no-user-gesture-required',
    ],
  });
  const context = await browser.newContext({
    viewport: { width: 1680, height: 1050 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    permissions: ['microphone'],
  });
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push('pageerror: ' + String(error)));

  const sessionCreates = [];
  page.on('response', async (response) => {
    const request = response.request();
    if (request.method() === 'POST' && new URL(response.url()).pathname === '/api/live/sessions') {
      try {
        sessionCreates.push(await response.json());
      } catch {
        /* ignore */
      }
    }
  });
  const wsFrames = [];
  page.on('websocket', (ws) => {
    if (!ws.url().includes('/api/live/sessions/')) return;
    ws.on('framereceived', (frame) => {
      try {
        const payload = JSON.parse(frame.payload);
        wsFrames.push({
          type: payload.type,
          resumed: payload.resumed,
          base_ms: payload.base_ms,
          total_ms: payload.total_ms,
          connection: payload.connection,
        });
      } catch {
        /* binary frame */
      }
    });
  });

  try {
    // ---------------- 登录 ----------------
    await page.goto(base + '/login', { waitUntil: 'domcontentloaded' });
    await sleep(800);
    await page.locator('input[placeholder="请输入用户名"]').fill('admin');
    await page.locator('input[placeholder="请输入密码"]').fill('admin123');
    await page.getByRole('button', { name: /登\s*录|登录/ }).click();
    await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 15000 });
    check('login with admin/admin123', true);

    // ---------------- 录音页 + 实时字幕 ----------------
    await page.goto(base + '/record', { waitUntil: 'domcontentloaded' });
    const chip = page.locator('.live-status');
    const startButton = page.getByRole('button', { name: /开始录音/ });
    await startButton.waitFor({ timeout: 20000 });
    const bodyText = await page.locator('body').innerText();
    check('no "Beta" copy on record page', !/Beta/i.test(bodyText));
    await startButton.click();
    await chip.filter({ hasText: '实时字幕中' }).waitFor({ timeout: 25000 });
    check('live captions connected (real backend)', true, 'session=' + (sessionCreates[0] || {}).id);
    check(
      'session created with mock provider',
      sessionCreates.length === 1 && sessionCreates[0].provider === 'mock',
      JSON.stringify((sessionCreates[0] || {}).provider),
    );

    await sleep(4500);
    const linesBefore = await page.locator('.live-line:not(.live-line--partial)').count();
    check('final captions received before drop', linesBefore >= 1, 'lines=' + linesBefore);
    await shot(page, '05-live-captions-ga.png', '实时字幕（GA，无 Beta，mock provider）');

    const sessionId = sessionCreates[0].id;
    const fetchSession = () =>
      page.evaluate(async (id) => {
        const response = await fetch(`/api/live/sessions/${id}`);
        return response.json();
      }, sessionId);

    const beforeDrop = await fetchSession();
    check(
      'backend session active with segments',
      beforeDrop.session.status === 'active' && beforeDrop.segments.length >= 1,
      `status=${beforeDrop.session.status} segs=${beforeDrop.segments.length}`,
    );

    // ---------------- 断线 #1：代理掐断 WS ----------------
    const dropped = proxy.dropLiveSockets();
    check('proxy dropped the live WebSocket', dropped === 1, 'dropped=' + dropped);
    await chip.filter({ hasText: '重连中' }).waitFor({ timeout: 10000 });
    check('reconnecting status after unexpected drop', true);
    await shot(page, '06-live-reconnect.png', '实时字幕断线重连中（自动 1s/2s/4s 退避）');
    await chip.filter({ hasText: '实时字幕中' }).waitFor({ timeout: 15000 });
    const resumedReady = wsFrames.filter((frame) => frame.type === 'ready' && frame.resumed === true);
    check('reconnect #1 resumed the same session', resumedReady.length >= 1, JSON.stringify(resumedReady[0] || {}));
    check('resume continues from audio base', (resumedReady[0] || {}).base_ms > 0, 'base_ms=' + (resumedReady[0] || {}).base_ms);
    check('server connection counter increased', (resumedReady[0] || {}).connection >= 2, 'connection=' + (resumedReady[0] || {}).connection);
    const sessionAfterResume = await fetchSession();
    check(
      'backend session still active after resume',
      sessionAfterResume.session.status === 'active',
      'status=' + sessionAfterResume.session.status,
    );

    // ---------------- 断线 #2：首次重连失败后再恢复 ----------------
    const drop2 = proxy.dropLiveSockets();
    proxy.setRejectNewConnections(true);
    await chip.filter({ hasText: '重连中' }).waitFor({ timeout: 10000 });
    await sleep(1500); // 1s 退避的重连被代理拒绝
    proxy.setRejectNewConnections(false);
    await chip.filter({ hasText: '实时字幕中' }).waitFor({ timeout: 15000 });
    const resumesAfterDrop2 = wsFrames.filter((frame) => frame.type === 'ready' && frame.resumed === true).length;
    check('retry after a failed attempt resumes the session', drop2 === 1 && resumesAfterDrop2 >= 2, `resumes=${resumesAfterDrop2}`);

    const linesBeforeSecond = await page.locator('.live-line:not(.live-line--partial)').count();
    await sleep(4200);
    const linesAfter = await page.locator('.live-line:not(.live-line--partial)').count();
    check('captions continue after reconnects', linesAfter > linesBeforeSecond, `${linesBeforeSecond} -> ${linesAfter}`);

    // ---------------- 正常停止 ----------------
    await page.getByRole('button', { name: /结束录音/ }).click();
    await chip.filter({ hasText: '实时稿已结束' }).waitFor({ timeout: 15000 });
    await sleep(800);
    const finalSession = await fetchSession();
    check('session ended after stop', finalSession.session.status === 'ended', 'status=' + finalSession.session.status);
    check(
      'live transcript persisted with audio',
      finalSession.segments.length >= 2 && finalSession.session.total_audio_ms > 0,
      `segs=${finalSession.segments.length} audio=${finalSession.session.total_audio_ms}ms`,
    );
    const segments = finalSession.segments;
    const contiguous = segments.every(
      (segment, index) => index === 0 || segment.start_ms === segments[index - 1].end_ms,
    );
    const uniqueTexts = new Set(segments.map((segment) => segment.text)).size === segments.length;
    check('resumed segments are contiguous', contiguous, segments.map((s) => `${s.start_ms}-${s.end_ms}`).join(','));
    check('no duplicated live captions', uniqueTexts);

    // ---------------- 主动停止后不再重连 ----------------
    const readyCount = wsFrames.filter((frame) => frame.type === 'ready').length;
    const createsCount = sessionCreates.length;
    await sleep(2500);
    check(
      'no reconnect after intentional stop',
      wsFrames.filter((frame) => frame.type === 'ready').length === readyCount && sessionCreates.length === createsCount,
      `ready=${readyCount} creates=${createsCount}`,
    );

    const realErrors = pageErrors.filter((entry) => !/net::ERR|Failed to fetch/.test(entry));
    check('no page errors', realErrors.length === 0, realErrors.slice(0, 3).join(' | '));
  } catch (error) {
    check('live reconnect scenario completed without exception', false, String(error && error.stack ? error.stack : error).split('\n')[0]);
  } finally {
    writeFileSync(
      REPORT_PATH,
      JSON.stringify({ upstreamPort: UPSTREAM_PORT, proxyBase: base, results }, null, 2),
      'utf-8',
    );
    await browser.close().catch(() => {});
    await proxy.close().catch(() => {});
  }

  const failed = results.filter((row) => !row.ok);
  console.log(`\n[summary] ${results.length - failed.length}/${results.length} checks passed → ${REPORT_PATH}`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((error) => {
  console.error('SCRIPT ERROR:', error);
  process.exit(2);
});
