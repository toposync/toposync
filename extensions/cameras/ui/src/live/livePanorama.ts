export const livePanoramaStyles = `.livePanorama { color: var(--text); height: 100%; min-height: 420px; display: flex; flex-direction: column; background: var(--bg); box-sizing: border-box; }
.livePanoramaEmpty { margin: auto; max-width: 440px; padding: 24px; color: var(--muted); text-align: center; }
.livePanoramaControls { position: absolute; top: 14px; left: 64px; right: 14px; display: flex; align-items: center; gap: 8px; pointer-events: none; }
.livePanoramaControls .iconButton { pointer-events: auto; }
.livePanoramaStatus { min-height: 36px; display: inline-flex; align-items: center; padding: 0 12px; border: 1px solid var(--border); border-radius: 999px; background: color-mix(in srgb, var(--panelSolid) 88%, transparent); box-shadow: 0 14px 38px rgba(0,0,0,.32), inset 0 1px 0 rgba(255,255,255,.10); backdrop-filter: blur(var(--glassBlur)) saturate(var(--glassSaturate)); }
.livePanoramaStop { margin-left: auto; }
.livePanoramaFooter { position: absolute; bottom: 0; left: 0; right: 0; min-height: 48px; display: flex; align-items: center; gap: 12px; padding: 6px 14px; box-sizing: border-box; border-top: 1px solid var(--border); background: color-mix(in srgb, var(--panelSolid) 88%, transparent); backdrop-filter: blur(var(--glassBlur)) saturate(var(--glassSaturate)); font-size: 12px; }
.livePanoramaAlert { color: var(--color-warning); }
.livePanoramaReconnect { margin-left: auto; flex: 0 0 auto; }
.livePanoramaFloatingPanel { position: absolute; overflow: hidden; border: 1px solid var(--borderStrong); border-radius: 12px; background: color-mix(in srgb, var(--panelSolid) 94%, transparent); box-shadow: 0 16px 42px rgba(0,0,0,.38), inset 0 1px 0 rgba(255,255,255,.08); backdrop-filter: blur(var(--glassBlur)) saturate(var(--glassSaturate)); transition: opacity .16s ease, box-shadow .16s ease; }
.livePanoramaFloatingPanel:focus-within { border-color: color-mix(in srgb, var(--color-focus-ring) 64%, var(--borderStrong)); }
.livePanoramaFloatingHeader { height: 36px; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 4px 0 12px; box-sizing: border-box; border-bottom: 1px solid var(--border); font-size: 12px; cursor: move; touch-action: none; user-select: none; }
.livePanoramaFloatingTitle { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.livePanoramaPanelButton.iconButton { width: 28px; height: 28px; flex: 0 0 28px; border-radius: 8px; box-shadow: none; }
.livePanoramaFloatingBody { background: #000; }
.livePanoramaResizeHandle { position: absolute; right: 0; bottom: 0; width: 34px; height: 34px; cursor: nwse-resize; touch-action: none; }
.livePanoramaResizeHandle::after { content: ''; position: absolute; right: 5px; bottom: 5px; width: 14px; height: 14px; border-right: 2px solid color-mix(in srgb, var(--text) 66%, transparent); border-bottom: 2px solid color-mix(in srgb, var(--text) 66%, transparent); border-radius: 0 0 4px 0; box-sizing: border-box; }
.livePanoramaResizeHandle:focus-visible { outline: 2px solid var(--color-focus-ring); outline-offset: -3px; }
@media (max-width: 720px) { .livePanoramaFooter { align-items: flex-start; flex-wrap: wrap; } .livePanoramaReconnect { margin-left: 0; } }
`;
