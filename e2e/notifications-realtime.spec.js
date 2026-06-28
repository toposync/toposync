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
    class MockEventSource {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSED = 2;

      constructor(url) {
        this.url = String(url);
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
      events.some((event) => event.kind === "open" && /\/api\/notifications\/stream$/.test(event.url)) &&
      events.some((event) => event.kind === "open" && /\/api\/notifications\/notif-two\/stream$/.test(event.url))
    );
  });

  await page.locator(".notificationCard", { hasText: "First realtime notification" }).click();

  await page.waitForFunction(() => {
    const events = window.__toposyncEventSourceEvents || [];
    return (
      events.some((event) => event.kind === "close" && /\/api\/notifications\/notif-two\/stream$/.test(event.url)) &&
      events.some((event) => event.kind === "open" && /\/api\/notifications\/notif-one\/stream$/.test(event.url))
    );
  });

  const events = await page.evaluate(() => window.__toposyncEventSourceEvents);
  const globalOpens = events.filter((event) => event.kind === "open" && /\/api\/notifications\/stream$/.test(event.url));
  const globalCloses = events.filter((event) => event.kind === "close" && /\/api\/notifications\/stream$/.test(event.url));
  const detailOpens = events.filter((event) => event.kind === "open" && /\/api\/notifications\/[^/]+\/stream$/.test(event.url));

  expect(globalOpens).toHaveLength(1);
  expect(globalCloses).toHaveLength(0);
  expect(detailOpens.map((event) => event.url).join("\n")).toContain("/api/notifications/notif-two/stream");
  expect(detailOpens.map((event) => event.url).join("\n")).toContain("/api/notifications/notif-one/stream");
});
