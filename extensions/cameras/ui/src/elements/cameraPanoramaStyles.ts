export const cameraPanoramaStyles = `
.cameraPanoramaMapping { min-width: 0; display: flex; flex-direction: column; gap: var(--space-4); color: var(--color-text-primary); }
.cameraPanoramaMapping button:disabled { opacity: 0.45; cursor: not-allowed; box-shadow: none; }
.cameraPanoramaPointSection { display: flex; flex-direction: column; gap: var(--space-2); }
.cameraPanoramaExploration { display: flex; flex-direction: column; gap: var(--space-1); }
.cameraPanoramaDiagnostics { padding: var(--space-3); border: 1px solid var(--color-border-strong); border-radius: var(--radius-control); display: flex; flex-direction: column; gap: var(--space-2); }
.cameraPanoramaDiagnostics ul { margin: 0; padding-inline-start: var(--space-5); }
.cameraPanoramaResiduals li { margin-block: var(--space-1); }
.cameraPanoramaActivationStatus { flex-shrink: 0; font-size: var(--font-size-12); padding-top: var(--space-2); max-height: 4.5em; overflow: auto; }
.cameraPanoramaMapping .card { margin: 0; }
.cameraPanoramaMapping p { margin: 0; line-height: var(--line-height-relaxed); }
.cameraPanoramaMapping button { min-height: 44px; min-width: 44px; }
.cameraPanoramaMapping .input, .cameraPanoramaMapping summary { min-height: 44px; }
.cameraPanoramaMapping summary { display: list-item; align-content: center; }
.cameraPanoramaPanel > .modalHeader .iconButton { min-width: 44px; min-height: 44px; }
.cameraPanoramaPanel > .modalBody { padding: clamp(16px, 2vw, 24px); }
.cameraPanoramaContent { display: flex; flex-direction: column; gap: var(--space-4); overflow-y: auto; min-height: 0; scroll-padding-block: 16px; padding: 2px; }

.cameraPanoramaMapping :focus-visible { outline: 2px solid var(--color-accent-teal); outline-offset: 3px; }
.cameraPanoramaInstruction { flex-shrink: 0; margin-bottom: var(--space-4); display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-3); padding: var(--space-3) var(--space-4); background: var(--color-accent-background-soft); border-radius: var(--radius-control); }
.cameraPanoramaInstruction h2 { font-size: var(--font-size-16); margin: 0 0 var(--space-1); }
.cameraPanoramaViewSwitch { display: none; }
.cameraPanoramaViews { flex-shrink: 0; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-3); }
.cameraPanoramaView { overflow: clip; min-width: 0; border: 1px solid var(--color-border-strong); border-radius: var(--radius-panel); background: var(--color-surface-solid); }
.cameraPanoramaView.isAwaiting { border-color: var(--color-accent-teal); box-shadow: 0 0 0 1px var(--color-accent-border); }
.cameraPanoramaViewHeader { position: sticky; top: 0; z-index: 2; background: var(--color-surface-solid); display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-2); padding: var(--space-3); min-height: 50px; border-bottom: 1px solid var(--color-border-subtle); }
.cameraPanoramaViewHeader h3 { font-size: var(--font-size-14); margin: 0; }
.cameraPanoramaViewport { height: clamp(280px, 43vh, 540px); overflow: hidden; position: relative; background: var(--color-canvas2d-background); }
.cameraPanoramaViewport .viewportCanvas { z-index: 0; }
.cameraPanoramaImage { position: relative; width: 100%; min-height: 100%; display: flex; align-items: center; justify-content: center; }
.cameraPanoramaImage.hasCoverage { min-height: 0; }
.cameraPanoramaImageContent { position: relative; width: 100%; }
.cameraPanoramaImageContent img { width: 100%; height: auto; display: block; user-select: none; }
.cameraPanoramaMarker { position: absolute; transform: translate(-50%, -50%) scale(var(--viewport-inverse-scale, 1)); min-height: 44px !important; width: 44px; height: 44px; padding: 0; border: 0; border-radius: 50%; background: transparent; color: var(--color-surface-solid); box-shadow: none; z-index: 1; font-size: 12px; font-weight: 700; display: grid; place-items: center; cursor: pointer; }
/* Pointer selection goes through the viewport so drags and pinches cannot select a pair. Keyboard buttons remain available. */
button.cameraPanoramaMarker { pointer-events: none; }
.cameraPanoramaMarker::before { content: ""; position: absolute; width: 26px; height: 26px; border: 2px solid var(--color-surface-solid); border-radius: 50%; background: var(--color-text-primary); box-shadow: var(--shadow-elevation-1); z-index: -1; }
.cameraPanoramaMarker.isPending { outline: 2px dashed var(--color-surface-solid); outline-offset: 2px; pointer-events: none; }
.cameraPanoramaEmpty { display: grid; place-items: center; gap: var(--space-3); text-align: center; padding: var(--space-8); min-height: 220px; color: var(--color-text-muted); }
.cameraPanoramaEmpty i { font-size: 32px; color: var(--color-accent-teal); }
.cameraPanoramaMapping details { border-top: 1px solid var(--color-border-subtle); padding-top: var(--space-3); }
.cameraPanoramaMapping summary { cursor: pointer; font-weight: var(--font-weight-medium); padding-block: var(--space-1); }
.cameraPanoramaPairs { display: flex; flex: 1; padding: 0; margin: 0; list-style: none; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
.cameraPanoramaPair { display: flex; align-items: center; border: 1px solid var(--color-border-strong); border-radius: var(--radius-control); overflow: hidden; }
.cameraPanoramaPair button { position: relative; display: grid; place-items: center; width: 48px; height: 48px; border: 0; background: transparent; color: inherit; padding: 0; cursor: pointer; }
.cameraPanoramaPairNumber { font-size: var(--font-size-18, 18px); font-weight: 700; line-height: 1; }
.cameraPanoramaPairState { position: absolute; right: 3px; top: 3px; font-size: 9px; }
.cameraPanoramaPair.isSelected { border-color: var(--color-accent-teal); background: var(--color-accent-background-soft); }
.cameraPanoramaFooter { flex-shrink: 0; margin-top: var(--space-3); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: var(--space-3); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-control); padding: var(--space-3); background: var(--color-surface-solid); box-shadow: var(--shadow-elevation-1); }
.cameraPanoramaActions { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2); }
.cameraPanoramaError { padding: var(--space-3); border: 1px solid var(--color-danger); border-radius: var(--radius-control); display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; }
.cameraPanoramaProgress { width: 100%; accent-color: var(--color-accent-teal); height: 8px; }
.cameraPanoramaCheckImage { max-height: 320px; width: 100%; object-fit: contain; display: block; background: var(--color-canvas2d-background); }
.cameraPanoramaTarget { position: relative; width: fit-content; max-width: 100%; margin-inline: auto; }
.cameraPanoramaTarget .cameraPanoramaCheckImage { width: auto; height: auto; max-width: 100%; max-height: 320px; object-fit: fill; }
.cameraPanoramaTargetCross { position: absolute; left: 50%; top: 50%; width: 28px; height: 28px; border: 2px solid var(--color-surface-solid); border-radius: 50%; transform: translate(-50%, -50%) scale(var(--viewport-inverse-scale, 1)); box-shadow: 0 0 0 1px var(--color-text-primary); pointer-events: none; }
.cameraPanoramaSaved { font-size: var(--font-size-12); color: var(--color-text-muted); }
.cameraPanoramaPointNavigation { min-width: 0; display: flex; flex-shrink: 0; align-items: center; gap: var(--space-2); margin-bottom: var(--space-2); }
.cameraPanoramaPointNavigation .cameraPanoramaPairs { min-width: 0; flex-wrap: nowrap; overflow-x: auto; padding: 3px; }
.cameraPanoramaPointNavigation .cameraPanoramaPair, .cameraPanoramaPointNavigation > .cameraPanoramaActions { flex-shrink: 0; }
.cameraPanoramaPointNavigation > button { flex-shrink: 0; }
.cameraPanoramaMarker.isSelected { outline: 2px solid var(--color-accent-teal); outline-offset: 2px; pointer-events: none; }
.cameraPanoramaGhost { position: absolute; width: 24px; height: 24px; transform: translate(-50%, -50%) scale(var(--viewport-inverse-scale, 1)); border: 2px dashed var(--color-text-primary); outline: 2px solid var(--color-surface-solid); border-radius: 50%; pointer-events: none; }
.cameraPanoramaSourcePreview { width: 100%; max-height: 300px; object-fit: contain; }
@media (max-width: 760px) {
  .cameraPanoramaFooter button { padding-inline: var(--space-2); white-space: normal; }

  .cameraPanoramaInstruction { padding: var(--space-3); gap: var(--space-2); }
  .cameraPanoramaPointNavigation { gap: var(--space-1); }
  .cameraPanoramaViewSwitch { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .cameraPanoramaViewSwitch [aria-pressed="true"] { border-color: var(--color-accent-teal); background: var(--color-accent-background-soft); }
  .cameraPanoramaViews { grid-template-columns: minmax(0, 1fr); }
  .cameraPanoramaViews > section { grid-area: 1 / 1; }
  .cameraPanoramaViews.mobile-image > section:nth-child(2), .cameraPanoramaViews.mobile-plan > section:nth-child(1) { visibility: hidden; pointer-events: none; }

  .cameraPanoramaViewport { height: 270px; }
  .cameraPanoramaImage.hasCoverage { min-height: 0; }
}

.cameraPanoramaInstruction { background: transparent; padding: var(--space-2) 0; margin-bottom: var(--space-2); }
.cameraPanoramaMapping .chipButton, .cameraPanoramaMapping .iconButton { box-shadow: none; }
.cameraPanoramaMapping .primaryButton { box-shadow: none; }
.cameraPanoramaFooter { box-sizing: border-box; width: 100%; box-shadow: none; }
.cameraPanoramaFooter .cameraPanoramaActivationStatus { flex: 1 1 240px; padding: 0; height: 3.5em; max-height: none; overflow: auto; }
.cameraPanoramaPreviewStatus { flex-shrink: 0; height: 3.5em; overflow: auto; }
.cameraPanoramaExploration > p { font-size: var(--font-size-14); }
.cameraPanoramaDiagnostics { background: var(--color-accent-background-soft); border-color: var(--color-border-subtle); }
.cameraPanoramaGhost { z-index: 3; border-style: solid; width: 22px; height: 22px; }
.cameraPanoramaGhost::after { content: ""; position: absolute; left: 50%; top: 50%; width: 3px; height: 3px; transform: translate(-50%, -50%); background: var(--color-text-primary); outline: 1px solid var(--color-surface-solid); border-radius: 50%; }
.cameraPanoramaGhost.hasPulse::before { content: ""; position: absolute; inset: -3px; border: 2px solid var(--color-accent-teal); border-radius: 50%; animation: panoramaProbePulse 850ms ease-out both; }
.cameraPanoramaViews.isCalculating .cameraPanoramaGhost { opacity: .4; }
@keyframes panoramaProbePulse { from { transform: scale(1); opacity: 1; } to { transform: scale(2.8); opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .cameraPanoramaGhost.hasPulse::before { animation: none; } }
`;
