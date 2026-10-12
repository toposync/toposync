const { test, expect } = require("@playwright/test");

const NOTIFICATIONS = [
  {
    id: "notif-one",
    type: "security.event",
    title: "First realtime notification",
    description: "First detail",
    priority: "high",
    createdAt: "2026-06-28T10:00:00.000Z",
    updatedAt: "2026-06-28T10:00:00.000Z",
    payload: { priority: "high", status: "open" },
  },
  {
    id: "notif-two",
    type: "security.event",
    title: "Second realtime notification",
    description: "Second detail",
    priority: "high",
    createdAt: "2026-06-28T10:01:00.000Z",
    updatedAt: "2026-06-28T10:01:00.000Z",
    payload: { priority: "high", status: "open" },
  },
];

async function mockNotificationsApi(page) {
  await page.route("**/api/notifications/count", async (route) => {
    await route.fulfill({
      json: {
        total: NOTIFICATIONS.length,
        by_priority: { low: 0, medium: 0, high: NOTIFICATIONS.length },
        unread_total: 0,
        unread_by_priority: { low: 0, medium: 0, high: 0 },
      },
    });
  });

  await page.route(/\/api\/notifications\/[^/]+$/, async (route) => {
    const url = new URL(route.request().url());
    const notificationId = decodeURIComponent(url.pathname.split("/").pop() || "");
    const notification = NOTIFICATIONS.find((item) => item.id === notificationId);
    if (!notification) {
      await route.fulfill({ status: 404, json: { detail: "Unknown notification" } });
      return;
    }
    await route.fulfill({ json: notification });
  });

  await page.route(/\/api\/notifications(?:\?.*)?$/, async (route) => {
    await route.fulfill({ json: { notifications: NOTIFICATIONS, next_cursor: null } });
  });
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "en");
    localStorage.setItem("toposync.theme", "default");
    localStorage.setItem("toposync.render_mode.v1", "2d");

    window.__toposyncEventSourceEvents = [];
    window.__toposyncEventSources = [];
    class MockEventSource {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSED = 2;

      constructor(url) {
        this.url = String(url);
        window.__toposyncEventSources.push(this);
        this.readyState = MockEventSource.CONNECTING;
        this.onopen = null;
        this.onmessage = null;
        this.onerror = null;
        window.__toposyncEventSourceEvents.push({ kind: "open", url: this.url });
        setTimeout(() => {
          if (this.readyState === MockEventSource.CLOSED) return;
          this.readyState = MockEventSource.OPEN;
          if (typeof this.onopen === "function") this.onopen(new Event("open"));
        }, 0);
      }

      addEventListener() {}

      removeEventListener() {}

      close() {
        if (this.readyState === MockEventSource.CLOSED) return;
        this.readyState = MockEventSource.CLOSED;
        window.__toposyncEventSourceEvents.push({ kind: "close", url: this.url });
      }
    }
    window.EventSource = MockEventSource;
  });
});

test("main screen keeps global and selected-notification SSE separate", async ({ page }) => {
  await mockNotificationsApi(page);

  await page.goto("/");
  await expect(page.getByText("First realtime notification")).toBeVisible();
  await expect(page.getByText("Second realtime notification")).toBeVisible();

  await page.waitForFunction(() => {
    const events = window.__toposyncEventSourceEvents || [];
    return (
      events.some((event) => event.kind === "open" && /\/api\/notifications\/stream(?:\?.*)?$/.test(event.url)) &&
      events.some((event) => event.kind === "open" && /\/api\/notifications\/notif-two\/stream(?:\?.*)?$/.test(event.url))
    );
  });

  await page.locator(".notificationCard", { hasText: "First realtime notification" }).click();

  await page.waitForFunction(() => {
    const events = window.__toposyncEventSourceEvents || [];
    return (
      events.some((event) => event.kind === "close" && /\/api\/notifications\/notif-two\/stream(?:\?.*)?$/.test(event.url)) &&
      events.some((event) => event.kind === "open" && /\/api\/notifications\/notif-one\/stream(?:\?.*)?$/.test(event.url))
    );
  });

  const events = await page.evaluate(() => window.__toposyncEventSourceEvents);
  const globalOpens = events.filter((event) => event.kind === "open" && /\/api\/notifications\/stream(?:\?.*)?$/.test(event.url));
  const globalCloses = events.filter((event) => event.kind === "close" && /\/api\/notifications\/stream(?:\?.*)?$/.test(event.url));
  const detailOpens = events.filter((event) => event.kind === "open" && /\/api\/notifications\/[^/]+\/stream(?:\?.*)?$/.test(event.url));

  expect(globalOpens).toHaveLength(1);
  expect(globalCloses).toHaveLength(0);
  expect(detailOpens.map((event) => event.url).join("\n")).toContain("/api/notifications/notif-two/stream");
  expect(detailOpens.map((event) => event.url).join("\n")).toContain("/api/notifications/notif-one/stream");
});

for (const recovery of ["reconnect", "overflow"]) {
  test(`notification history recovers missed inserts after ${recovery}`, async ({ page }) => {
    await mockNotificationsApi(page);
    let current = [NOTIFICATIONS[0]];
    let countReads = 0;
    await page.route("**/api/notifications/count", route => {
      countReads++;
      return route.fulfill({ json: { total: current.length,
        by_priority: { low: 0, medium: 0, high: current.length },
        unread_total: 0, unread_by_priority: { low: 0, medium: 0, high: 0 } } });
    });
    await page.route(/\/api\/notifications(?:\?.*)?$/, route =>
      route.fulfill({ json: { notifications: current, next_cursor: null } }));
    await page.goto("/");
    await expect(page.getByText("First realtime notification")).toBeVisible();
    await expect(page.getByText("Second realtime notification")).toHaveCount(0);
    const before = countReads;
    current = NOTIFICATIONS;
    await page.evaluate(kind => {
      const stream = window.__toposyncEventSources.find(source => /\/api\/notifications\/stream$/.test(source.url));
      if (kind === "reconnect") stream.onopen(new Event("open"));
      else stream.onmessage(new MessageEvent("message", { data: JSON.stringify({ op: "resync" }) }));
    }, recovery);
    await expect(page.getByText("Second realtime notification")).toBeVisible();
    await expect.poll(() => countReads).toBeGreaterThan(before);
    await page.screenshot({ path: test.info().outputPath(`recovery-${recovery}.png`) });
  });
}

test("server-resolved history refreshes low-priority inserts and updates without reconnecting", async ({ page }) => {
  await mockNotificationsApi(page);
  const low = NOTIFICATIONS.map(item => ({...item,priority:'low',payload:{...item.payload,priority:'low'}}));
  let current = [low[0]];
  await page.route(/\/api\/notifications(?:\?.*)?$/, route => route.fulfill({json:{notifications:current,next_cursor:null,
    search:{mode:'literal',status:'ready',time_range:{timezone:'America/Sao_Paulo',created_after:1,created_before:2,basis:'notification_created_at',expression:'hoje'}}}}));
  await page.goto('/');
  await expect(page.getByText('First realtime notification')).toBeVisible();
  current = low;
  await page.evaluate(notification => {
    const stream=window.__toposyncEventSources.find(source=>/\/api\/notifications\/stream$/.test(source.url));
    stream.onmessage(new MessageEvent('message',{data:JSON.stringify({op:'insert',notification})}));
  },low[1]);
  await expect(page.getByText('Second realtime notification')).toBeVisible();
  current = [low[1]];
  await page.evaluate(notification => {
    const stream=window.__toposyncEventSources.find(source=>/\/api\/notifications\/stream$/.test(source.url));
    stream.onmessage(new MessageEvent('message',{data:JSON.stringify({op:'update',notification})}));
  },{...low[0],description:'No longer in the selected period'});
  await expect(page.locator('.notificationCard',{hasText:'First realtime notification'})).toHaveCount(0);
  await expect(page.locator('.notificationCard',{hasText:'Second realtime notification'})).toHaveCount(1);
});

test("object category selector uses the extension route and refreshes on events", async ({ page }) => {
  await mockNotificationsApi(page);
  let reads = 0;
  await page.route(/\/api\/vision\/history\/notifications\?.*/, route => {
    expect(new URL(route.request().url()).searchParams.get('category')).toBe('dog');
    reads++;
    return route.fulfill({json:{notifications:[NOTIFICATIONS[0]],next_cursor:null,search:{mode:'literal',status:'ready'}}});
  });
  await page.goto('/');
  await page.getByRole('button',{name:'Search events',exact:true}).click();
  await page.getByRole('combobox',{name:'Detected object',exact:true}).selectOption('dog');
  await expect.poll(() => reads).toBeGreaterThan(0);
  const before = reads;
  await page.evaluate(notification => {
    const stream=window.__toposyncEventSources.find(source=>/\/api\/notifications\/stream$/.test(source.url));
    stream.onmessage(new MessageEvent('message',{data:JSON.stringify({op:'update',notification})}));
  },NOTIFICATIONS[0]);
  await expect.poll(() => reads).toBeGreaterThan(before);
  await page.screenshot({path:test.info().outputPath('category-filter.png')});
});

test("semantic history signals changes without repeating inference automatically", async ({ page }) => {
  await mockNotificationsApi(page);
  let reads = 0;
  await page.route(/\/api\/ai\/history\/search\?.*/, route => {
    reads++;
    return route.fulfill({json:{notifications:[NOTIFICATIONS[0]],next_cursor:null,search:{mode:'semantic_text',status:'ready'}}});
  });
  await page.goto('/');
  await page.getByRole('button',{name:'Search events',exact:true}).click();
  await page.getByRole('searchbox',{name:'Search',exact:true}).fill('person');
  await page.getByRole('combobox',{name:'Search in',exact:true}).selectOption('text');
  await expect.poll(() => reads).toBeGreaterThan(0);
  await page.getByRole('dialog').getByRole('button',{name:'Close',exact:true}).click();
  const before = reads;
  await page.evaluate(notification => {
    const stream=window.__toposyncEventSources.find(source=>/\/api\/notifications\/stream$/.test(source.url));
    stream.onmessage(new MessageEvent('message',{data:JSON.stringify({op:'insert',notification})}));
  },NOTIFICATIONS[1]);
  await expect(page.getByText('There are new or updated events.')).toBeVisible();
  await page.waitForTimeout(800);
  expect(reads).toBe(before);
  await page.getByRole('button',{name:'Refresh results',exact:true}).click();
  await expect.poll(() => reads).toBeGreaterThan(before);
});

test("reset clears an unapplied custom period and its validation error", async ({ page }) => {
  await mockNotificationsApi(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Search events', exact: true }).click();
  const period = page.getByRole('combobox', { name: 'Period', exact: true });
  await period.selectOption('custom');
  await page.getByLabel('Start date', { exact: true }).fill('2026-06-29');
  await page.getByLabel('End date (inclusive)', { exact: true }).fill('2026-06-28');
  await page.getByRole('button', { name: 'Apply interval', exact: true }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await page.getByRole('button', { name: 'Reset to defaults', exact: true }).click();
  await expect(period).toHaveValue('all');
  await expect(page.getByRole('alert')).toHaveCount(0);
  await period.selectOption('custom');
  await expect(page.getByLabel('Start date', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('End date (inclusive)', { exact: true })).toHaveValue('');
});

test("period-only history keeps both pages during realtime updates and excludes other days", async ({ page }) => {
  await mockNotificationsApi(page);
  const notifications = Array.from({ length: 80 }, (_, index) => ({
    ...NOTIFICATIONS[0], id: `period-${index}`, title: `Period event ${index}`,
    createdAt: new Date(Date.UTC(2026, 5, 28, 10, index)).toISOString(),
  }));
  let reads = 0;
  await page.route(/\/api\/notifications(?:\?.*)?$/, route => {
    reads++;
    const params = new URL(route.request().url()).searchParams;
    const next = params.has('before');
    return route.fulfill({ json: { notifications: notifications.slice(next ? 0 : 40, next ? 40 : 80),
      next_cursor: next ? null : Date.parse(notifications[40].createdAt) / 1000 } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Search events', exact: true }).click();
  await page.getByRole('combobox', { name: 'Period', exact: true }).selectOption('custom');
  await page.getByLabel('Start date', { exact: true }).fill('2026-06-28');
  await page.getByLabel('End date (inclusive)', { exact: true }).fill('2026-06-28');
  await page.getByRole('button', { name: 'Apply interval', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
  const list = page.locator('.railScroll').filter({ has: page.locator('.notificationCard') });
  await list.hover();
  await page.mouse.wheel(0, 10000);
  await expect(page.locator('.notificationCard')).toHaveCount(80);
  const before = reads;
  await page.evaluate(items => {
    const stream = window.__toposyncEventSources.find(source => /\/api\/notifications\/stream$/.test(source.url));
    for (const notification of items) {
      stream.onmessage(new MessageEvent('message', { data: JSON.stringify({ op: 'update', notification }) }));
    }
  }, [
    { ...notifications[0], title: 'Updated older page event' },
    { ...notifications[0], id: 'in-period', title: 'New in-period event' },
    { ...notifications[0], id: 'outside-period', title: 'Outside period event', createdAt: '2026-06-27T10:00:00Z' },
  ]);
  await expect(page.getByText('Updated older page event', { exact: true })).toHaveCount(1);
  await expect(page.getByText('New in-period event', { exact: true })).toHaveCount(1);
  await page.waitForTimeout(800);
  await expect(page.locator('.notificationCard')).toHaveCount(81);
  await expect(page.getByText('Outside period event', { exact: true })).toHaveCount(0);
  expect(reads).toBe(before);
});
