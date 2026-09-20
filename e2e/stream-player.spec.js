const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

let clip;
test.beforeAll(() => {
  const directory = path.resolve('.toposync-data/stream-player-validation');
  fs.mkdirSync(directory, { recursive: true });
  const file = path.join(directory, 'clock.mp4');
  execFileSync('ffmpeg', [
    '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
    '-i', 'testsrc=size=160x90:rate=15', '-t', '30', '-c:v', 'libx264',
    '-profile:v', 'baseline', '-level:v', '3.0', '-pix_fmt', 'yuv420p',
    '-g', '15', '-movflags', 'empty_moov+frag_keyframe+default_base_moof', file,
  ]);
  clip = fs.readFileSync(file);
});

test('real host MSE keeps decoding through token renewal and recovers a closed connection', async ({ page }) => {
  const connections = [];
  await page.route('**/api/**', route => route.fulfill({ json: {} }));
  await page.routeWebSocket('**/fixture-mse*', socket => {
    connections.push(socket);
    socket.onMessage(() => {
      socket.send(JSON.stringify({ type: 'mse', value: 'video/mp4; codecs="avc1.42C01E"' }));
      socket.send(clip);
    });
  });
  await page.goto('/');
  const video = page.locator('video');
  await expect.poll(() => video.evaluate(element => element.currentTime)).toBeGreaterThan(.5);
  const before = await video.evaluate(element => ({ time: element.currentTime, source: element.currentSrc }));

  await page.getByRole('button', { name: 'Renovar credencial' }).click();
  await expect.poll(() => video.evaluate(element => element.currentTime)).toBeGreaterThan(before.time + .5);
  expect(await video.evaluate(element => element.currentSrc)).toBe(before.source);
  expect(connections).toHaveLength(1);

  connections[0].close({ code: 1011, reason: 'Controlled connection failure' });
  await expect.poll(() => connections.length).toBe(2);
  expect(connections[1].url()).toContain('media_token=renewed');
  await expect.poll(() => video.evaluate(element => element.currentTime)).toBeGreaterThan(.5);
  expect(await video.evaluate(element => element.currentSrc)).not.toBe(before.source);

  await page.getByRole('button', { name: 'Trocar fonte' }).click();
  await expect.poll(() => connections.length).toBe(3);
  expect(connections[2].url()).toContain('source=other');
  await expect.poll(() => video.evaluate(element => element.currentTime)).toBeGreaterThan(.5);

  await page.getByRole('button', { name: 'Alternar atividade' }).click();
  await expect.poll(() => video.evaluate(element => element.paused && element.getAttribute('src') === null)).toBe(true);
});

test('warmup refusal retries once and a later decoder failure starts a fresh session', async ({ page }) => {
  const connections = [];
  await page.route('**/api/**', route => route.fulfill({ json: {} }));
  await page.routeWebSocket('**/fixture-mse*', socket => {
    connections.push(socket);
    const warmup = connections.length === 1;
    socket.onMessage(() => {
      if (warmup) {
        socket.send(JSON.stringify({ type: 'error', value: 'source is unavailable' }));
        return;
      }
      socket.send(JSON.stringify({ type: 'mse', value: 'video/mp4; codecs="avc1.42C01E"' }));
      socket.send(clip);
    });
  });
  await page.goto('/');
  const video = page.locator('video');
  await expect.poll(() => video.evaluate(element => element.currentTime)).toBeGreaterThan(.5);
  expect(connections).toHaveLength(2);
  await video.evaluate(element => element.dispatchEvent(new Event('error')));
  await expect.poll(() => connections.length).toBe(3);
  await expect.poll(() => video.evaluate(element => element.currentTime)).toBeGreaterThan(.5);
});
