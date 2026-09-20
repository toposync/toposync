const assert = require("node:assert/strict");
const http = require("node:http");
const { test } = require("node:test");
const { createProxyMiddleware } = require("http-proxy-middleware");
const webpackConfig = require("../webpack.config.js");

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function fixture(t, handler, compress = false) {
  const sockets = new Set();
  const backend = http.createServer(handler);
  const frontend = http.createServer();
  for (const server of [backend, frontend]) server.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    await Promise.all([backend, frontend].map((server) => new Promise((resolve) => server.close(resolve))));
  });
  await new Promise((resolve) => backend.listen(0, "127.0.0.1", resolve));
  const config = webpackConfig({}, { mode: "development" }).devServer.proxy[0];
  const middleware = createProxyMiddleware({ ...config,
    target: `http://127.0.0.1:${backend.address().port}`, logLevel: "silent" });
  const compression = compress ? require("compression")() : null;
  frontend.on("request", compression
    ? (request, response) => compression(request, response, () => middleware(request, response))
    : middleware);
  await new Promise((resolve) => frontend.listen(0, "127.0.0.1", resolve));
  return `http://127.0.0.1:${frontend.address().port}/api/notifications/stream`;
}

test("no-transform streams reach gzip-capable clients without compression buffering", { timeout: 4000 }, async (t) => {
  const expected = "data: " + JSON.stringify({ frame: "fresh" }) + "\n\n";
  const url = await fixture(t, (_request, response) => {
    response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-store, no-transform" });
    response.write(expected); // Deliberately keep the response open.
  }, true);
  await new Promise((resolve, reject) => {
    const client = http.get(url, { headers: { "Accept-Encoding": "gzip" } }, (response) => {
      assert.equal(response.headers["content-encoding"], undefined);
      response.once("data", (chunk) => {
        assert.equal(chunk.toString(), expected);
        response.destroy(); client.destroy(); resolve();
      });
    });
    client.on("error", reject);
  });
});

test("closing a downstream SSE response releases its upstream connection", { timeout: 4000 }, async (t) => {
  let backendClosed = false;
  const url = await fixture(t, (_request, response) => {
    response.on("close", () => { backendClosed = true; });
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    response.write("event: ready\ndata: {}\n\n");
  });
  await new Promise((resolve, reject) => {
    const client = http.get(url, (response) => response.once("data", () => {
      response.destroy();
      client.destroy();
      resolve();
    }));
    client.on("error", reject);
  });
  for (let attempt = 0; attempt < 50 && !backendClosed; attempt++) await delay(20);
  assert.equal(backendClosed, true, "completed GET requests must not orphan an infinite SSE response");
});

test("disconnect before upstream response headers also releases upstream", { timeout: 4000 }, async (t) => {
  let backendClosed = false;
  let arrived;
  const arrival = new Promise((resolve) => { arrived = resolve; });
  const url = await fixture(t, (_request, response) => {
    response.on("close", () => { backendClosed = true; });
    arrived();
  });
  const client = http.get(url);
  client.on("error", () => {});
  await arrival;
  client.destroy();
  for (let attempt = 0; attempt < 50 && !backendClosed; attempt++) await delay(20);
  assert.equal(backendClosed, true);
});

test("normally completed responses retain their complete body", { timeout: 4000 }, async (t) => {
  const expected = "measured-data-".repeat(10000);
  const url = await fixture(t, (_request, response) => {
    response.writeHead(200, { "Content-Type": "text/plain" });
    response.end(expected);
  });
  const actual = await new Promise((resolve, reject) => {
    const client = http.get(url, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve(Buffer.concat(chunks).toString()));
      response.on("error", reject);
    });
    client.on("error", reject);
  });
  assert.equal(actual, expected);
});
