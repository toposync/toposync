const { test, expect } = require("@playwright/test");

const point = (x, z, gap = false) => ({ x, z, ts: x + z, composition_id: "spatial-fixture", scope: "capture-one", ...(gap ? { gap_before: true } : {}) });
const records = [
  { id: "curve", type: "pipelines.tracking", title: "Independent curves", priority: "high", createdAt: "2026-10-12T20:00:00Z", updatedAt: "2026-10-12T20:00:00Z", payload: {
    source: "pipelines", status: "closed", priority: "high", world: point(33, 12), spatial_history: { version: 1, trails: [
      {subject_id: "a", points: [point(30, 10), point(32, 10), point(32, 12), point(33, 12)]},
      {subject_id: "b", points: [point(-12, 8), point(-10, 8), point(-9, 9, true), point(-8, 9)]},
    ] },
  } },
  { id: "single", type: "pipelines.tracking", title: "Single observed point", priority: "high", createdAt: "2026-10-12T19:00:00Z", updatedAt: "2026-10-12T19:00:00Z", payload: { source: "pipelines", status: "closed", priority: "high", world: point(20, 8) } },
];

async function fixture(page) {
  const writes = [];
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "en");
    localStorage.setItem("toposync.render_mode.v1", "3d");
    window.__notificationStreams = [];
    window.EventSource = class {
      constructor(url) { this.url = String(url); this.readyState = 1; window.__notificationStreams.push(this); }
      addEventListener() {} removeEventListener() {} close() { this.readyState = 2; }
    };
  });
  await page.route("**/api/**", route => {
    if (!["GET", "HEAD"].includes(route.request().method())) { writes.push(route.request().url()); return route.abort(); }
    return route.continue();
  });
  await page.route("**/api/extensions", route => route.fulfill({json: []}));
  await page.route("**/api/compositions", route => route.fulfill({json: {active_composition_id: "spatial-fixture", compositions:[{id:"spatial-fixture",name:"Spatial fixture"}]}}));
  await page.route("**/api/composition", route => route.fulfill({json: {id:"spatial-fixture",name:"Spatial fixture",elements:[{
    id:"reference",type:"com.toposync.core.measurement.line",position:{x:0,y:0,z:0},rotation:{x:0,y:0,z:0},props:{points:[{x:0,z:0},{x:2,z:0}]},
  }]}}));
  await page.route("**/api/files/exists?**", route => route.fulfill({json:{exists:true}}));
  await page.route(/\/files\/render2d\/.*_manifest.json$/, route => route.fulfill({json:{version:9,compositionId:"spatial-fixture",signature:"fixture",widthPx:1000,heightPx:1000,bounds:{minX:-5,maxX:5,minZ:-5,maxZ:5},base:{url:"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='1000' height='1000'%3E%3Crect width='1000' height='1000' fill='%23273543'/%3E%3C/svg%3E"},overlays:[]}}));
  await page.route("**/api/notifications/count", route => route.fulfill({json:{total:2,by_priority:{high:2,medium:0,low:0},unread_total:0,unread_by_priority:{high:0,medium:0,low:0}}}));
  await page.route(/\/api\/notifications(?:\?.*)?$/, route => route.fulfill({json:{notifications:records,next_cursor:null}}));
  await page.route(/\/api\/notifications\/(curve|single)$/, route => route.fulfill({json:records.find(record=>route.request().url().endsWith(record.id))}));
  return writes;
}
async function mode(page, label) {
  await page.getByRole("button",{name:/^Rendering:/}).click();
  await page.getByRole("dialog",{name:"Rendering"}).getByText(label,{exact:true}).click();
}
async function verifyPixels(page) {
  const readPins = () => page.locator(".notification2dPinShape").evaluateAll(elements => elements.map(element => {
    const pin = element.getBoundingClientRect(), viewport = element.closest(".viewportRoot").getBoundingClientRect();
    const rail = document.querySelector(".overlayLeft.isOpen")?.getBoundingClientRect();
    return {inside: pin.left >= viewport.left && pin.right <= viewport.right && pin.top >= viewport.top && pin.bottom <= viewport.bottom,
      occluded: rail && pin.left < rail.right && pin.right > rail.left && pin.top < rail.bottom && pin.bottom > rail.top};
  }));
  await expect.poll(readPins).toEqual([{inside:true,occluded:false}]);
  const points = await page.locator(".notification2dTrail path").evaluateAll(elements => elements.flatMap(element => {
    const viewport = element.closest(".viewportRoot").getBoundingClientRect();
    return [...element.getAttribute("d").matchAll(/[ML] (-?[\d.]+) (-?[\d.]+)/g)].map(match=>({x:Number(match[1]),y:Number(match[2]),width:viewport.width,height:viewport.height}));
  }));
  for (const point of points) { expect(point.x).toBeGreaterThan(0); expect(point.x).toBeLessThan(point.width); expect(point.y).toBeGreaterThan(0); expect(point.y).toBeLessThan(point.height); }
}

test("independent trajectories fit in both 2D views, preserve manual navigation and survive 3D remounts", async ({page}, testInfo) => {
  const errors=[]; page.on("pageerror", error=>errors.push(error.message));
  const writes=await fixture(page);
  await page.goto("/");
  await page.getByRole("button",{name:/Independent curves/}).click();
  for (const label of ["2D (Plan)", "2D (Image)"]) {
    await mode(page,label);
    await expect(page.locator(".notification2dPinShape")).toBeVisible();
    await verifyPixels(page);
    await expect(page.locator(".notification2dTrail path")).toHaveAttribute("d", /M .* M .* M /);
    await page.screenshot({path:testInfo.outputPath(`${label.includes("Plan")?"plan":"image"}.png`)});
    await page.setViewportSize({width:390,height:900});
    await verifyPixels(page);
    await page.setViewportSize({width:1280,height:720});
    await verifyPixels(page);
    const before=await page.locator(".notification2dPin").getAttribute("style");
    const viewport=await page.locator(".notificationSpatialViewport .viewportRoot").boundingBox();
    await page.mouse.move(viewport.x+viewport.width*.6,viewport.y+viewport.height*.5); await page.mouse.wheel(0,-120);
    await expect.poll(()=>page.locator(".notification2dPin").getAttribute("style")).not.toBe(before);
    const manual=await page.locator(".notification2dPin").getAttribute("style");
    await page.evaluate(record=>{
      for (const stream of window.__notificationStreams) if (stream.url.includes("/curve/stream") && stream.onmessage) stream.onmessage({data:JSON.stringify({op:"update",notification:record})});
      window.dispatchEvent(new Event("toposync:invalidate"));
    }, {...records[0],title:"Independent curves updated",updatedAt:"2026-10-12T20:01:00Z"});
    await expect(page.getByRole("button",{name:/Independent curves updated/})).toBeVisible();
    await page.setViewportSize({width:1200,height:800});
    await expect(page.locator(".notification2dPin")).toHaveAttribute("style",manual);
    await page.getByRole("button",{name:"Frame occurrence",exact:true}).click();
    await verifyPixels(page);
    await page.getByRole("button",{name:/Single observed point/}).click();
    await expect(page.locator(".notification2dTrail path")).toHaveCount(0);
    await verifyPixels(page);
    await page.getByRole("button",{name:/Independent curves/}).click();
    await page.setViewportSize({width:390,height:900});
    await page.getByRole("button",{name:"Frame occurrence",exact:true}).click();
    await verifyPixels(page);
    await page.setViewportSize({width:1280,height:720});
    await mode(page,"3D");
    await expect(page.locator("canvas")).toBeVisible();
    await page.getByRole("button",{name:"Frame occurrence",exact:true}).click();
    await page.locator("canvas").screenshot({path:testInfo.outputPath("3d.png")});
    const coloredPixels = await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => {
      const canvas = document.querySelector(".notificationSpatialViewport canvas");
      const gl = canvas.getContext("webgl2");
      const pixels = new Uint8Array(gl.drawingBufferWidth * gl.drawingBufferHeight * 4);
      gl.readPixels(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      let count = 0;
      for (let index = 0; index < pixels.length; index += 4) if (pixels[index] > 90 && pixels[index] > pixels[index + 1] * 1.6 && pixels[index] > pixels[index + 2] * 1.6) count += 1;
      resolve(count);
    })));
    expect(coloredPixels).toBeGreaterThan(10);
  }
  expect(errors).toEqual([]); expect(writes).toEqual([]);
});

test("detail resync during an initial fetch refreshes durable evidence over HTTP", async ({page}) => {
  const writes = await fixture(page);
  let requests = 0;
  let release;
  const initialFetch = new Promise(resolve => { release = resolve; });
  const updated = {...records[0], title:"HTTP recovered evidence", updatedAt:"2026-10-12T20:03:00Z"};
  let current = records[0];
  await page.route(/\/api\/notifications(?:\?.*)?$/, route => route.fulfill({json:{notifications:[current,records[1]],next_cursor:null}}));
  await page.route("**/api/notifications/curve", async route => {
    requests += 1;
    if (requests === 1) { await initialFetch; return route.fulfill({json:records[0]}); }
    return route.fulfill({json:updated});
  });
  await page.goto("/");
  await page.getByRole("button",{name:/Independent curves/}).click();
  await expect.poll(()=>requests).toBe(1);
  current = updated;
  await page.evaluate(()=>{
    for (const stream of window.__notificationStreams) if(stream.url.includes("/curve/stream") && stream.onmessage)
      stream.onmessage({data:JSON.stringify({op:"resync"})});
  });
  release();
  await expect(page.getByRole("button",{name:/HTTP recovered evidence/})).toBeVisible();
  expect(requests).toBe(2);
  expect(writes).toEqual([]);
});
