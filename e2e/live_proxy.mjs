// Echoic QA helper: 可主动断开 WebSocket 的 TCP 代理（用于实时字幕断线重连测试）
//
// 用法（Node ESM）：
//   import { createLiveProxy } from './live_proxy.mjs'
//   const proxy = await createLiveProxy({ upstreamPort: 8100 })
//   // 用 http://127.0.0.1:${proxy.port} 打开 Echoic（HTTP/WS 全部经代理）
//   proxy.dropLiveSockets()          // 模拟网络中断：关闭代理侧的 WS TCP 连接
//   proxy.setRejectNewConnections(true) // 让重连立即失败（测试退避重试）
//   await proxy.close()
import net from 'node:net';

export async function createLiveProxy({
  upstreamHost = '127.0.0.1',
  upstreamPort = 8100,
  proxyPort = 0, // 0 = 随机空闲端口
} = {}) {
  const wsSockets = [];
  let rejectNewConnections = false;
  const sockets = new Set();

  const server = net.createServer((client) => {
    sockets.add(client);
    if (rejectNewConnections) {
      client.destroy();
      sockets.delete(client);
      return;
    }
    const upstream = net.connect(upstreamPort, upstreamHost);
    sockets.add(upstream);
    upstream.setNoDelay(true);
    client.setNoDelay(true);
    const state = { client, upstream, isWs: false, head: '' };

    const cleanup = () => {
      const index = wsSockets.indexOf(state);
      if (index >= 0) wsSockets.splice(index, 1);
      sockets.delete(client);
      sockets.delete(upstream);
      client.destroy();
      upstream.destroy();
    };

    client.on('error', cleanup);
    upstream.on('error', cleanup);
    client.on('close', cleanup);
    upstream.on('close', cleanup);

    client.on('data', (chunk) => {
      if (state.isWs || state.head.length > 16384) return;
      state.head += chunk.toString('latin1');
      if (/Upgrade:\s*websocket/i.test(state.head) && /\/api\/live\/sessions\/\d+\/stream/.test(state.head)) {
        state.isWs = true;
        wsSockets.push(state);
        state.head = '';
      }
    });

    client.pipe(upstream);
    upstream.pipe(client);
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(proxyPort, '127.0.0.1', () => resolve());
  });
  const port = server.address().port;

  return {
    port,
    baseUrl: `http://127.0.0.1:${port}`,
    dropLiveSockets() {
      const dropped = wsSockets.splice(0);
      for (const state of dropped) {
        state.client.destroy();
        state.upstream.destroy();
      }
      return dropped.length;
    },
    setRejectNewConnections(value) {
      rejectNewConnections = Boolean(value);
    },
    async close() {
      for (const socket of sockets) socket.destroy();
      sockets.clear();
      await new Promise((resolve) => server.close(() => resolve()));
    },
  };
}

export default createLiveProxy;
