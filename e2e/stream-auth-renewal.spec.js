const { test, expect } = require("@playwright/test");

// Requires an authenticated, isolated fixture with an existing synthetic live view.
const liveView = process.env.TOPOSYNC_TEST_SYNTHETIC_LIVE_VIEW;

for (const acceptsProbe of [false, true]) {
  test(`HLS authorization renewal stays bounded when probe acceptance is ${acceptsProbe}`, async ({ page }) => {
    test.skip(!liveView, "A synthetic live view must be explicitly selected");
    await page.addInitScript(() => localStorage.setItem("toposync.locale", "en"));
    let playlists = 0;
    let playbacks = 0;
    let acceptedProbe = false;
    page.on("request", request => {
      if (new URL(request.url()).pathname.endsWith(`/live-views/${liveView}/playback`)) playbacks++;
    });
    await page.route("**/api/streams/media/hls/**", async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith("segment.ts")) {
        acceptedProbe = true;
        return route.fulfill({ status: 206, body: Buffer.from([0x47, 0]) });
      }
      playlists++;
      if (playlists > 12) return route.abort();
      if (acceptsProbe && playlists === 2) {
        return route.fulfill({ contentType: "application/vnd.apple.mpegurl", body:
          "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:1,\nsegment.ts\n" });
      }
      return route.fulfill({ status: 401, json: { detail: "media_token_invalid" } });
    });
    await page.goto(`./cameras/live/${encodeURIComponent(liveView)}`);
    await expect.poll(() => playbacks).toBe(2);
    // Observe recovery timers too: accepting a probe is not successful playback.
    await page.waitForTimeout(8000);
    expect(playbacks).toBe(2);
    expect(playlists).toBeLessThanOrEqual(8);
    expect(acceptedProbe).toBe(acceptsProbe);
    expect(await page.locator("video").evaluate(video => video.readyState)).toBeLessThan(2);
    await page.mouse.move(10, 100);
    await page.getByRole("button", { name: "Show analysis", exact: true }).click();
    await expect(page.getByRole("complementary", { name: "Show analysis", exact: true })).toBeVisible();
  });
}
