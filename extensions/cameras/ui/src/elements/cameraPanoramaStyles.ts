export const cameraPanoramaStyles = `
.cameraPanoramaMapping { display: flex; flex-direction: column; gap: var(--space-4); color: var(--color-text-primary); }
.cameraPanoramaMapping .card { margin: 0; }
.cameraPanoramaMapping p { margin: 0; line-height: var(--line-height-relaxed); }
.cameraPanoramaMapping button { min-height: 44px; min-width: 44px; }
.cameraPanoramaMapping .input, .cameraPanoramaMapping summary { min-height: 44px; }
.cameraPanoramaMapping summary { display: list-item; align-content: center; }
.cameraPanoramaPanel > .modalHeader .iconButton { min-width: 44px; min-height: 44px; }
:root[data-toposync-base-theme="topo-day"] .cameraPanoramaMapping .primaryButton { color: var(--color-text-primary); }
.cameraPanoramaMapping :focus-visible { outline: 2px solid var(--color-accent-teal); outline-offset: 3px; }
.cameraPanoramaInstruction { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-3); padding: var(--space-3) var(--space-4); background: var(--color-accent-background-soft); }
.cameraPanoramaInstruction h2 { font-size: var(--font-size-16); margin: 0 0 var(--space-1); }
.cameraPanoramaViews { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-3); }
.cameraPanoramaView { overflow: hidden; min-width: 0; border: 1px solid var(--color-border-strong); border-radius: var(--radius-panel); background: var(--color-surface-solid); }
.cameraPanoramaView.isAwaiting { border-color: var(--color-accent-teal); box-shadow: 0 0 0 1px var(--color-accent-border); }
.cameraPanoramaViewHeader { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-2); padding: var(--space-3); min-height: 50px; border-bottom: 1px solid var(--color-border-subtle); }
.cameraPanoramaViewHeader h3 { font-size: var(--font-size-14); margin: 0; }
.cameraPanoramaViewport { height: clamp(260px, 34vh, 380px); overflow: auto; position: relative; background: var(--color-canvas2d-background); }
.cameraPanoramaImage { position: relative; width: 100%; min-height: 100%; display: flex; align-items: center; justify-content: center; }
.cameraPanoramaImage.hasCoverage { min-height: 0; }
.cameraPanoramaImageContent { position: relative; width: 100%; }
.cameraPanoramaImageContent img { width: 100%; height: auto; display: block; user-select: none; }
.cameraPanoramaMarker { position: absolute; transform: translate(-50%, -50%) scale(var(--viewport-inverse-scale, 1)); min-height: 44px !important; width: 44px; height: 44px; padding: 0; border: 0; border-radius: 50%; background: transparent; color: var(--color-surface-solid); box-shadow: none; z-index: 1; font-size: 12px; font-weight: 700; display: grid; place-items: center; cursor: pointer; }
.cameraPanoramaMarker::before { content: ""; position: absolute; width: 26px; height: 26px; border: 2px solid var(--color-surface-solid); border-radius: 50%; background: var(--color-text-primary); box-shadow: var(--shadow-elevation-1); z-index: -1; }
.cameraPanoramaMarker.isCheck::before { border-radius: 5px; }
.cameraPanoramaMarker.isPending { outline: 2px dashed var(--color-surface-solid); outline-offset: 2px; pointer-events: none; }
.cameraPanoramaEmpty { display: grid; place-items: center; gap: var(--space-3); text-align: center; padding: var(--space-8); min-height: 220px; color: var(--color-text-muted); }
.cameraPanoramaEmpty i { font-size: 32px; color: var(--color-accent-teal); }
.cameraPanoramaMapping details { border-top: 1px solid var(--color-border-subtle); padding-top: var(--space-3); }
.cameraPanoramaMapping summary { cursor: pointer; font-weight: var(--font-weight-medium); padding-block: var(--space-1); }
.cameraPanoramaPairs { display: flex; flex: 1; padding: 0; margin: 0; list-style: none; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
.cameraPanoramaPair { display: flex; align-items: center; border: 1px solid var(--color-border-strong); border-radius: var(--radius-control); overflow: hidden; }
.cameraPanoramaPair button { display: flex; align-items: center; gap: var(--space-2); border: 0; border-radius: 0; background: transparent; color: inherit; padding: 5px 10px; cursor: pointer; }
.cameraPanoramaPair.isSelected { border-color: var(--color-accent-teal); background: var(--color-accent-background-soft); }
.cameraPanoramaFooter { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: var(--space-3); border-top: 1px solid var(--color-border-subtle); padding-top: var(--space-3); }
.cameraPanoramaActions { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2); }
.cameraPanoramaError { padding: var(--space-3); border: 1px solid var(--color-danger); border-radius: var(--radius-control); display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; }
.cameraPanoramaProgress { width: 100%; accent-color: var(--color-accent-teal); height: 8px; }
.cameraPanoramaCheckImage { max-height: 320px; width: 100%; object-fit: contain; display: block; background: var(--color-canvas2d-background); }
.cameraPanoramaTarget { position: relative; width: fit-content; max-width: 100%; margin-inline: auto; }
.cameraPanoramaTarget .cameraPanoramaCheckImage { width: auto; height: auto; max-width: 100%; max-height: 320px; object-fit: fill; }
.cameraPanoramaTargetCross { position: absolute; left: 50%; top: 50%; width: 28px; height: 28px; border: 2px solid var(--color-surface-solid); border-radius: 50%; transform: translate(-50%, -50%) scale(var(--viewport-inverse-scale, 1)); box-shadow: 0 0 0 1px var(--color-text-primary); pointer-events: none; }
.cameraPanoramaSaved { font-size: var(--font-size-12); color: var(--color-text-muted); }
.cameraPanoramaPointNavigation { display: flex; align-items: start; gap: var(--space-3); }
.cameraPanoramaPointNavigation > button { flex-shrink: 0; }
.cameraPanoramaPointRole { font-size: var(--font-size-12); }
.cameraPanoramaViews.hasExpandedView { grid-template-columns: minmax(0, 1fr); }
.cameraPanoramaView[hidden] { display: none; }
.cameraPanoramaMarker.isSelected { outline: 2px solid var(--color-accent-teal); outline-offset: 2px; pointer-events: none; }
.cameraPanoramaGhost { position: absolute; width: 24px; height: 24px; transform: translate(-50%, -50%) scale(var(--viewport-inverse-scale, 1)); border: 2px dashed var(--color-text-primary); outline: 2px solid var(--color-surface-solid); border-radius: 50%; pointer-events: none; }
.cameraPanoramaSourcePreview { width: 100%; max-height: 300px; object-fit: contain; }
@media (max-width: 760px) {
  .cameraPanoramaPointNavigation { flex-wrap: wrap; }
  .cameraPanoramaViews { grid-template-columns: minmax(0, 1fr); }
  .cameraPanoramaViewport { height: 270px; }
  .cameraPanoramaImage.hasCoverage { min-height: 0; }
}
`;
