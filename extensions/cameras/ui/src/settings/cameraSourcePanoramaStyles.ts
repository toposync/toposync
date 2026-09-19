export const cameraSourcePanoramaStyles = `
.sourcePanorama { margin-block: var(--space-4); min-width: 0; }
.sourcePanorama > .cardBody, .sourcePanoramaCrop { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }
.sourcePanorama h2, .sourcePanorama h3, .sourcePanorama p, .sourcePanorama figure { margin: 0; }
.sourcePanorama h2 { font-size: var(--font-size-18); font-weight: var(--font-weight-semibold); }
.sourcePanorama h3 { font-size: var(--font-size-16); font-weight: var(--font-weight-semibold); }
.sourcePanorama p { line-height: var(--line-height-relaxed); overflow-wrap: anywhere; }
.sourcePanoramaHeading, .sourcePanoramaJobHeading { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: var(--space-3); }
.sourcePanoramaHeading > div { display: grid; gap: var(--space-1); }
.sourcePanoramaHeading > div > p { color: var(--color-text-muted); font-size: var(--font-size-13); }
.sourcePanoramaActions, .sourcePanoramaFooter { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
.sourcePanoramaReconstruct { display: grid; gap: var(--space-2); flex: 1 1 260px; }
.sourcePanoramaReconstruct > button { justify-self: start; }
.sourcePanoramaFooter { justify-content: space-between; gap: var(--space-4); border-top: 1px solid var(--color-border-subtle); padding-top: var(--space-4); }
.sourcePanoramaFooter > p { max-width: 60ch; flex: 1 1 260px; }
.sourcePanorama button, .sourcePanorama a.chipButton, .sourcePanorama summary { min-width: 44px; min-height: 44px; box-sizing: border-box; }
.sourcePanorama button, .sourcePanorama a.chipButton { white-space: normal; }
.sourcePanorama a.chipButton { display: inline-flex; justify-content: center; align-items: center; text-decoration: none; }
.sourcePanorama :focus-visible { outline: 3px solid var(--color-accent-teal); outline-offset: 3px; }
.sourcePanorama button[aria-pressed=true] { border-color: var(--color-accent-border); background: var(--color-accent-background-strong); }
.sourcePanorama .primaryButton { color: var(--color-text-primary); background: var(--color-accent-background-strong); border-color: var(--color-accent-border); }
.sourcePanoramaEmpty { display: grid; justify-items: center; text-align: center; gap: var(--space-3); padding: var(--space-8) var(--space-4); }
.sourcePanoramaEmpty > p { max-width: 58ch; color: var(--color-text-muted); }
.sourcePanoramaEmpty > i { font-size: 30px; color: var(--color-text-muted); }
.sourcePanoramaJob { display: grid; gap: var(--space-3); padding: var(--space-4); border-radius: var(--radius-panel); background: var(--color-application-background-secondary); }
.sourcePanoramaMilestones { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--space-3); padding: 0; margin: 0 0 var(--space-2); list-style: none; }
.sourcePanoramaMilestones > li { display: flex; align-items: center; gap: var(--space-2); border-bottom: 2px solid var(--color-border-subtle); padding-block: var(--space-2); color: var(--color-text-muted); font-size: var(--font-size-13); }
.sourcePanoramaMilestones > li[aria-current=step] { color: var(--color-text-primary); border-color: var(--color-accent-teal); font-weight: var(--font-weight-semibold); }
.sourcePanoramaMilestones > li > span { display: grid; place-items: center; border: 1px solid var(--color-border-strong); border-radius: 50%; width: 24px; height: 24px; flex: 0 0 24px; }
.sourcePanoramaMilestones > li[data-complete=true] > span { border-color: var(--color-accent-teal); }
.sourcePanoramaPreview { display: grid; gap: var(--space-2); justify-items: center; }
.sourcePanoramaPreview > img { max-width: 100%; max-height: 300px; object-fit: contain; }
.sourcePanoramaPreview > figcaption { color: var(--color-text-muted); font-size: var(--font-size-12); }
.sourcePanoramaImage { position: relative; overflow: hidden; margin-inline: auto; background: var(--color-canvas2d-background); border-radius: var(--radius-control); }
.sourcePanoramaImage > img { display: block; position: absolute; max-width: none; object-fit: fill; }
.sourcePanoramaFacts { display: grid; gap: var(--space-2); margin: 0; font-size: var(--font-size-14); }
.sourcePanoramaFacts > div { display: grid; grid-template-columns: minmax(6em, max-content) minmax(0, 1fr); gap: var(--space-3); }
.sourcePanoramaFacts dt { font-weight: var(--font-weight-semibold); }
.sourcePanoramaFacts dd { margin: 0; line-height: var(--line-height-relaxed); overflow-wrap: anywhere; }
.sourcePanoramaNotice, .sourcePanoramaError { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; padding: var(--space-3) var(--space-4); border-radius: var(--radius-control); background: var(--color-application-background-secondary); }
.sourcePanoramaNotice > p, .sourcePanoramaError > p { flex: 1 1 230px; }
.sourcePanoramaError { border: 1px solid var(--color-danger); }
.sourcePanoramaSuccess { border-inline-start: 3px solid var(--color-accent-teal); padding-inline-start: var(--space-3); }
.sourcePanorama details { border-top: 1px solid var(--color-border-subtle); padding-top: var(--space-2); }
.sourcePanorama summary { display: list-item; align-content: center; cursor: pointer; font-weight: var(--font-weight-medium); }
.sourcePanorama details[open] > p, .sourcePanorama details[open] > .sourcePanoramaActions { margin-block-start: var(--space-2); }
.sourcePanoramaDetails { min-width: 0; max-width: 100%; }
.sourcePanoramaTelemetry { display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--space-3); width: 100%; min-width: 0; margin-block: var(--space-3); }
.sourcePanoramaTelemetry > *, .sourcePanoramaTelemetry figure { min-width: 0; max-width: 100%; }
.sourcePanoramaTelemetryPlot, .sourcePanoramaTelemetryTable { min-width: 0; width: 100%; max-width: 100%; overflow: auto; }
.sourcePanoramaTelemetryPlot svg { display: block; width: 100%; min-width: 480px; fill: var(--color-text-primary); font-size: 14px; }
.sourcePanoramaTelemetryPlot path { fill: none; stroke-width: 2; vector-effect: non-scaling-stroke; }
.sourcePanoramaTelemetryGrid { stroke: var(--color-border-subtle); stroke-width: 1; vector-effect: non-scaling-stroke; }
.sourcePanoramaTelemetryMotion { stroke: var(--color-text-primary); }
.sourcePanoramaTelemetryDrift { stroke: var(--color-accent-teal); stroke-dasharray: 7 4; }
.sourcePanoramaTelemetryConfidence { stroke: var(--color-text-primary); stroke-dasharray: 2 3; }
.sourcePanoramaTelemetry figcaption { font-size: var(--font-size-13); line-height: var(--line-height-relaxed); }
.sourcePanoramaTelemetryLegend { display: flex; flex-wrap: wrap; gap: var(--space-3); padding: 0; margin: 0; list-style: none; font-size: var(--font-size-13); }
.sourcePanoramaTelemetryLegend li { display: inline-flex; align-items: center; gap: var(--space-2); }
.sourcePanoramaTelemetryLegend span { display: inline-block; width: 28px; border-top: 2px solid var(--color-text-primary); }
.sourcePanoramaTelemetryLegend .sourcePanoramaTelemetryDrift { border-top-style: dashed; border-top-color: var(--color-accent-teal); }
.sourcePanoramaTelemetryLegend .sourcePanoramaTelemetryConfidence { border-top-style: dotted; }
.sourcePanoramaTelemetryEvents { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: var(--space-2) var(--space-3); margin: 0; font-size: var(--font-size-13); }
.sourcePanoramaTelemetryEvents dd { margin: 0; font-variant-numeric: tabular-nums; }
.sourcePanoramaTelemetryTable { max-height: 300px; }
.sourcePanoramaTelemetry table { width: 100%; border-collapse: collapse; font-size: var(--font-size-13); }
.sourcePanoramaTelemetry caption { text-align: left; padding-block: var(--space-2); }
.sourcePanoramaTelemetry th, .sourcePanoramaTelemetry td { padding: var(--space-2); text-align: left; border-bottom: 1px solid var(--color-border-subtle); font-variant-numeric: tabular-nums; }
.sourcePanoramaTelemetry th { min-width: 90px; }
.sourcePanoramaPreviewViewport { width: 100%; height: clamp(260px, 45vh, 520px); background: var(--color-canvas2d-background); border-radius: var(--radius-control); }
.sourcePanoramaCropViewport { width: 100%; height: clamp(260px, 45vh, 520px); border: 1px solid var(--color-border-strong); border-radius: var(--radius-control); background: var(--color-canvas2d-background); }
.sourcePanoramaCropStage { position: relative; overflow: hidden; touch-action: none; user-select: none; }
.sourcePanoramaCropStage.isDrawing { cursor: crosshair; }
.sourcePanoramaDialog { width: min(1240px, calc(100vw - 24px)); max-width: none; max-height: calc(100dvh - 24px); margin: auto; padding: 0; color: var(--color-text-primary); background: var(--color-surface-solid); border: 1px solid var(--color-border-strong); border-radius: var(--radius-modal); box-shadow: var(--shadow-elevation-3); }
.sourcePanoramaDialog::backdrop { background: var(--color-scrim); }
.sourcePanoramaDialog[open] { display: flex; }
.sourcePanoramaDialog > .sourcePanoramaCrop { width: 100%; max-height: calc(100dvh - 26px); gap: 0; }
.sourcePanoramaDialogHeader { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--color-border-subtle); flex: 0 0 auto; }
.sourcePanoramaDialogHeader > strong { font-size: var(--font-size-14); overflow-wrap: anywhere; }
.sourcePanoramaEditorContent { display: flex; flex-direction: column; gap: var(--space-4); min-height: 0; overflow: auto; padding: var(--space-4); }
.sourcePanoramaEditorContent > * { flex-shrink: 0; }
.sourcePanoramaEditorFooter { padding: var(--space-3) var(--space-4); flex: 0 0 auto; background: var(--color-surface-solid); }
.sourcePanoramaCloseNotice { margin: 0 var(--space-4) var(--space-4); flex: 0 0 auto; }
.sourcePanoramaStrip { position: absolute; inset: 0 auto 0 0; width: 200%; display: flex; pointer-events: none; }
.sourcePanoramaStrip > img { width: 50%; height: 100%; max-width: none; object-fit: fill; flex: 0 0 50%; }
.sourcePanoramaCropMask { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; fill: var(--color-text-primary); opacity: .5; }
.sourcePanoramaSelection { position: absolute; box-sizing: border-box; border: 2px solid var(--color-surface-solid); box-shadow: 0 0 0 1px var(--color-text-primary), inset 0 0 0 1px var(--color-text-primary); }
.sourcePanoramaHandle { position: absolute; transform: translate(-50%, -50%) scale(var(--viewport-inverse-scale, 1)); width: 44px; height: 44px; padding: 0; border: 0; background: transparent; z-index: 2; }
.sourcePanoramaHandle::after { content: ''; position: absolute; inset: 15px; background: var(--color-surface-solid); border: 1px solid var(--color-text-primary); border-radius: 2px; }
.sourcePanoramaHandle:focus-visible { outline-offset: -4px; background: var(--color-accent-background-strong); }
.sourcePanoramaFirstCorner { position: absolute; transform: translate(-50%, -50%) scale(var(--viewport-inverse-scale, 1)); width: 16px; height: 16px; border: 3px solid var(--color-surface-solid); outline: 1px solid var(--color-text-primary); border-radius: 50%; pointer-events: none; }
@media (max-width: 600px) {
  .sourcePanoramaDialog { width: calc(100vw - 8px); max-height: calc(100dvh - 8px); border-radius: var(--radius-control); }
  .sourcePanoramaDialog > .sourcePanoramaCrop { max-height: calc(100dvh - 10px); }
  .sourcePanoramaEditorContent { padding: var(--space-3); gap: var(--space-3); }
  .sourcePanoramaEditorFooter { padding: var(--space-3); gap: var(--space-2); }
  .sourcePanoramaEditorFooter > .sourcePanoramaActions { width: 100%; justify-content: flex-end; }
  .sourcePanoramaMilestones { gap: var(--space-2); }
  .sourcePanoramaMilestones > li { flex-direction: column; align-items: flex-start; }
  .sourcePanoramaJob { padding: var(--space-3); }
  .sourcePanoramaFooter > .primaryButton { width: 100%; }
}
@media (prefers-reduced-motion: reduce) {
  .sourcePanorama *, .sourcePanorama *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
}
`;
