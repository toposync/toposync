const { test, expect } = require('@playwright/test');
test.use({ actionTimeout: 12000 });
const fs = require('node:fs/promises');
const path = require('node:path');

// Real extension UI with synthetic API responses; never contacts a camera.
async function setup(page, legacy = false) {
  let profiles = legacy ? [{ id: 'old_attention', name: 'Previous profile', mode: 'shadow', resume_mode: null,
    camera_id: 'front', ptz_device_id: 'front', source_id: 'wide', composition_id: 'yard', home_view_id: 'home', eligible_view_ids: ['home'],
    event_policies: [{ event_type: 'security.person', enabled: true, priority: 10, preferred_view_id: 'home' }],
    same_head_observer_acknowledged: false, candidate_confirm_seconds: 3, min_focus_seconds: 10, max_focus_seconds: 120,
    close_grace_seconds: 6, cooldown_seconds: 10, stale_timeout_seconds: 18, settle_timeout_seconds: 8, lease_ttl_seconds: 15,
    max_movements_per_minute: 2, minimum_target_confidence: .5 }] : [];
  const saved = [], cameraWrites = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (/\/api\/cameras\//.test(request.url()) && request.method() !== 'GET') cameraWrites.push(request.url()); });
  await page.addInitScript(() => { localStorage.setItem('toposync.locale', 'en'); localStorage.setItem('toposync.theme', 'topo-day'); });
  await page.route('**/api/ptz-attention/**', async route => {
    const request = route.request(), pathname = new URL(request.url()).pathname;
    let response = {};
    if (pathname.endsWith('/catalog')) response = { generated_at: Date.now() / 1000, operator_id: 'ptz_attention.request', modes: ['shadow', 'live_panorama'], states: [],
      cameras: [{ id: 'front', name: 'Synthetic front camera', enabled: true, actuator_id: 'front', permissions: { configure: false, control: false },
        sources: [{ id: 'wide', name: 'Synthetic wide stream', enabled: true, kind: 'video', has_ptz: true }], views: [],
        panorama_mappings: [{ composition_id: 'yard', composition_name: 'Synthetic yard', element_id: 'camera-element', element_name: 'Front mapping', source_id: 'wide', ready: true }] }],
      event_types: [{ event_type: 'security.person', profile_ids: [] }], bindings: [], permissions: { configure: true, control: true }, services: {}, partial_errors: [] };
    else if (pathname.endsWith('/validate')) {
      const profile = request.postDataJSON() || profiles[0];
      const live = profile.mode === 'live_panorama';
      response = { ok: !live, profile_id: profile.id, mode: profile.mode, resolved_views: {}, resolved_targets: {}, services: {}, automation_ready: true,
        issues: [{ code: 'attention_physical_qualification_required', severity: live ? 'error' : 'warning', blocking: live, message: 'Physical qualification pending' }] };
    } else if (pathname.endsWith('/profiles') && request.method() === 'GET') response = { profiles };
    else if (pathname.includes('/profiles') && ['POST', 'PUT'].includes(request.method())) { response = request.postDataJSON(); saved.push(response); profiles = [response]; }
    else if (pathname.endsWith('/status')) response = { generated_at: Date.now() / 1000, devices: profiles.map(profile => ({ profile_id: profile.id, ptz_device_id: profile.ptz_device_id, state: "IDLE", lease_active: false, movements_last_minute: 0, pending_events: 0 })), services: {} };
    else if (pathname.includes('/decisions')) response = { decisions: [], next_cursor: null };
    await route.fulfill({ json: response });
  });
  await page.goto('/settings?panel=com.toposync.ptz_attention');
  await expect(page.getByRole('button', { name: 'New profile', exact: true })).toBeVisible();
  return { saved, cameraWrites, errors };
}

async function mapping(page, modal) {
  await modal.getByLabel(/PTZ camera/).selectOption('front');
  await modal.getByLabel(/PTZ control source/).selectOption('wide');
  await modal.getByLabel(/Panorama mapping/).selectOption('camera-element');
  await modal.getByLabel('Return point X', { exact: true }).fill('0');
  await modal.getByLabel('Return point Z', { exact: true }).fill('0');
  await modal.getByLabel(/Attention region/).fill('-2 -2\n2 -2\n2 2\n-2 2');
  await modal.getByRole('button', { name: 'Next', exact: true }).click();
  await modal.getByRole('button', { name: 'Next', exact: true }).click();
  await modal.getByLabel('Profile name', { exact: true }).fill('Panorama attention');
}

for (const width of [1440, 390]) test(`panorama profile can be saved in shadow and blocks unqualified motion at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  const { saved, cameraWrites, errors } = await setup(page);
  await page.getByRole('button', { name: 'New profile', exact: true }).click();
  const modal = page.getByRole('dialog', { name: 'Create PTZ Attention profile', exact: true });
  await modal.getByRole('button', { name: 'security.person', exact: true }).click();
  await modal.getByRole('button', { name: 'Next', exact: true }).click();
  await mapping(page, modal);
  await expect(modal.getByText('Physical qualification pending', { exact: true })).toBeVisible();
  await expect(modal.getByRole('button', { name: 'Save profile', exact: true })).toBeEnabled();
  await modal.getByRole('radio', { name: /Live movement/ }).check();
  await expect(modal.getByRole('button', { name: 'Save profile', exact: true })).toBeDisabled();
  await modal.getByRole('radio', { name: /^Shadow review/ }).check();
  await expect(modal.getByRole('button', { name: 'Save profile', exact: true })).toBeEnabled();
  const overflow = await modal.evaluate(element => element.scrollWidth - element.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  if (process.env.TOPOSYNC_ATTENTION_EVIDENCE_DIRECTORY) {
    await fs.mkdir(process.env.TOPOSYNC_ATTENTION_EVIDENCE_DIRECTORY, { recursive: true });
    await page.screenshot({ path: path.join(process.env.TOPOSYNC_ATTENTION_EVIDENCE_DIRECTORY, `attention-${width}.png`), fullPage: true });
  }
  await modal.getByRole('button', { name: 'Save profile', exact: true }).click();
  await expect(modal).not.toBeVisible();
  expect(saved).toHaveLength(1);
  expect(saved[0]).toMatchObject({ mode: 'shadow', panorama_element_id: 'camera-element', home_target: { x: 0, z: 0 }, home_view_id: '', eligible_view_ids: [] });
  expect(saved[0].attention_region).toHaveLength(4);
  expect(cameraWrites).toEqual([]);
  expect(errors).toEqual([]);
});

test('previous view profile requires explicit migration before saving panorama destinations', async ({ page }) => {
  const { saved, cameraWrites, errors } = await setup(page, true);
  await page.getByRole('button', { name: 'Edit profile', exact: true }).click();
  const modal = page.getByRole('dialog', { name: 'Edit PTZ Attention profile', exact: true });
  await modal.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(modal.getByText(/This profile uses old view destinations/)).toBeVisible();
  await modal.getByRole('button', { name: 'Replace view destinations', exact: true }).click();
  await mapping(page, modal);
  await expect(modal.getByRole('button', { name: 'Save profile', exact: true })).toBeEnabled();
  expect(saved).toEqual([]);
  await modal.getByRole('button', { name: 'Save profile', exact: true }).click();
  await expect(modal).not.toBeVisible();
  expect(saved[0].id).toBe('old_attention');
  expect(saved[0].minimum_target_confidence).toBe(0);
  expect(saved[0].event_policies[0].preferred_view_id).toBe('');
  expect(cameraWrites).toEqual([]);
  expect(errors).toEqual([]);
});
