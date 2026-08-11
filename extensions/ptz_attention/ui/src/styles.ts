const STYLE_ID = "toposync-ptz-attention-styles";

const styles = `
.ptzAttentionPanel { max-width: var(--content-max-width); margin: 0 auto; color: var(--color-text-primary); }
.ptzAttentionPanel, .ptzAttentionPanel * { box-sizing: border-box; }
.ptzAttentionHeader { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); margin-bottom: var(--space-4); }
.ptzAttentionHeaderActions { display: flex; flex-wrap: wrap; gap: var(--space-2); justify-content: flex-end; }
.ptzAttentionLayout { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: var(--space-4); align-items: start; }
.ptzAttentionMain, .ptzAttentionRail { min-width: 0; display: grid; gap: var(--space-4); }
.ptzAttentionCard { border: 1px solid var(--color-border-subtle); border-radius: var(--radius-panel); background: var(--color-surface-frost); box-shadow: var(--shadow-elevation-1); overflow: hidden; }
.ptzAttentionCardBody { padding: var(--space-4); min-width: 0; }
.ptzAttentionCardTitle { margin: 0; font-size: var(--font-size-16); font-weight: var(--font-weight-semibold); letter-spacing: var(--letter-spacing-tight); }
.ptzAttentionMuted { color: var(--color-text-muted); font-size: var(--font-size-13); line-height: var(--line-height-normal); overflow-wrap: anywhere; }
.ptzAttentionSubtle { color: var(--color-text-subtle); font-size: var(--font-size-12); }
.ptzAttentionHero { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: var(--space-4); align-items: start; }
.ptzAttentionSessionTitle { margin: var(--space-2) 0 0; font-size: clamp(22px, 4vw, 34px); font-weight: var(--font-weight-semibold); letter-spacing: -0.03em; overflow-wrap: anywhere; }
.ptzAttentionBadgeRow { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; }
.ptzAttentionBadge { display: inline-flex; align-items: center; gap: 6px; min-height: 28px; padding: 4px 10px; border: 1px solid var(--color-border-subtle); border-radius: var(--radius-pill); background: var(--color-surface-frost-strong); color: var(--color-text-muted); font-size: var(--font-size-12); font-weight: var(--font-weight-medium); }
.ptzAttentionBadge[data-tone="success"] { border-color: color-mix(in srgb, var(--color-success) 48%, transparent); color: var(--color-success); }
.ptzAttentionBadge[data-tone="warning"] { border-color: color-mix(in srgb, var(--color-warning) 48%, transparent); color: var(--color-warning); }
.ptzAttentionBadge[data-tone="danger"] { border-color: color-mix(in srgb, var(--color-danger) 48%, transparent); color: var(--color-danger); }
.ptzAttentionBadge[data-tone="info"] { border-color: color-mix(in srgb, var(--color-info) 48%, transparent); color: var(--color-info); }
.ptzAttentionTimeline { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--space-2); margin-top: var(--space-5); }
.ptzAttentionStage { min-width: 0; padding: var(--space-3); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-control); background: var(--color-surface-frost-strong); }
.ptzAttentionStage[data-active="true"] { border-color: var(--color-accent-border); background: var(--color-accent-background-soft); }
.ptzAttentionStage[data-complete="true"] { border-color: color-mix(in srgb, var(--color-success) 40%, transparent); }
.ptzAttentionStageNumber { display: grid; place-items: center; width: 24px; height: 24px; border-radius: var(--radius-pill); background: var(--color-application-background-secondary); color: var(--color-text-muted); font-size: var(--font-size-11); font-weight: var(--font-weight-semibold); }
.ptzAttentionStage[data-active="true"] .ptzAttentionStageNumber { background: var(--color-accent-teal); color: var(--color-text-inverted); }
.ptzAttentionStageLabel { margin-top: var(--space-2); font-size: var(--font-size-13); font-weight: var(--font-weight-semibold); overflow-wrap: anywhere; }
.ptzAttentionMetrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--space-2); margin-top: var(--space-4); }
.ptzAttentionMetric { min-width: 0; padding: var(--space-3); border-top: 1px solid var(--color-border-subtle); }
.ptzAttentionMetricValue { margin-top: 3px; font-size: var(--font-size-14); font-weight: var(--font-weight-medium); overflow-wrap: anywhere; }
.ptzAttentionActions { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-4); }
.ptzAttentionNotice { padding: var(--space-3); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-control); color: var(--color-text-muted); background: var(--color-surface-frost-strong); overflow-wrap: anywhere; }
.ptzAttentionNotice[data-tone="warning"] { border-color: color-mix(in srgb, var(--color-warning) 48%, transparent); }
.ptzAttentionNotice[data-tone="danger"] { border-color: color-mix(in srgb, var(--color-danger) 48%, transparent); }
.ptzAttentionNotice[data-tone="success"] { border-color: color-mix(in srgb, var(--color-success) 48%, transparent); }
.ptzAttentionProfileList, .ptzAttentionDecisionList, .ptzAttentionIssueList { display: grid; gap: var(--space-2); margin-top: var(--space-3); }
.ptzAttentionProfileButton { width: 100%; padding: var(--space-3); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-control); background: var(--color-surface-frost-strong); color: inherit; text-align: left; cursor: pointer; }
.ptzAttentionProfileButton:hover { border-color: var(--color-border-strong); }
.ptzAttentionProfileButton:focus-visible { outline: 2px solid var(--color-focus-ring-outer); outline-offset: 2px; }
.ptzAttentionProfileButton[data-selected="true"] { border-color: var(--color-accent-border); background: var(--color-accent-background-soft); }
.ptzAttentionProfileName { font-size: var(--font-size-14); font-weight: var(--font-weight-semibold); overflow-wrap: anywhere; }
.ptzAttentionDecision { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: var(--space-3); align-items: start; padding: var(--space-3) 0; border-top: 1px solid var(--color-border-subtle); }
.ptzAttentionDecision:first-child { border-top: 0; }
.ptzAttentionDecisionMark { width: 10px; height: 10px; margin-top: 5px; border-radius: var(--radius-pill); background: var(--color-text-subtle); }
.ptzAttentionDecisionMark[data-tone="success"] { background: var(--color-success); }
.ptzAttentionDecisionMark[data-tone="warning"] { background: var(--color-warning); }
.ptzAttentionDecisionMark[data-tone="danger"] { background: var(--color-danger); }
.ptzAttentionDecisionTime { max-width: 100%; color: var(--color-text-subtle); font-size: var(--font-size-11); overflow-wrap: anywhere; }
.ptzAttentionEmpty { padding: var(--space-8) var(--space-4); text-align: center; }
.ptzAttentionEmptyIcon { display: grid; place-items: center; width: 44px; height: 44px; margin: 0 auto var(--space-3); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-pill); color: var(--color-text-muted); }
.ptzAttentionWizard { width: min(940px, calc(100vw - 28px)); max-height: calc(100vh - 28px); }
.ptzAttentionWizardSteps { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--space-2); margin-bottom: var(--space-4); }
.ptzAttentionWizardStep { min-width: 0; padding: var(--space-3); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-control); background: transparent; color: var(--color-text-muted); text-align: left; }
.ptzAttentionWizardStep[data-current="true"] { border-color: var(--color-accent-border); background: var(--color-accent-background-soft); color: var(--color-text-primary); }
.ptzAttentionWizardStep:focus-visible { outline: 2px solid var(--color-focus-ring-outer); outline-offset: 2px; }
.ptzAttentionWizardBody { display: grid; gap: var(--space-4); min-width: 0; }
.ptzAttentionChoiceGrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr)); gap: var(--space-3); }
.ptzAttentionModeFieldset { min-width: 0; margin: var(--space-4) 0 0; padding: 0; border: 0; }
.ptzAttentionModeFieldset legend { margin-bottom: var(--space-2); color: var(--color-text-muted); font-size: var(--font-size-13); font-weight: var(--font-weight-medium); }
.ptzAttentionWizard .modalTitle { margin: 0; }
.ptzAttentionChoice { display: block; min-width: 0; padding: var(--space-3); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-control); background: var(--color-surface-frost-strong); }
.ptzAttentionChoice label, label.ptzAttentionChoice { cursor: pointer; }
.ptzAttentionChoice[data-selected="true"] { border-color: var(--color-accent-border); background: var(--color-accent-background-soft); }
.ptzAttentionChoice:focus-within { outline: 2px solid var(--color-focus-ring-outer); outline-offset: 2px; }
.ptzAttentionChoice input { margin-right: var(--space-2); }
.ptzAttentionChoiceTitle { font-size: var(--font-size-14); font-weight: var(--font-weight-semibold); overflow-wrap: anywhere; }
.ptzAttentionChoiceMeta { margin-top: 5px; color: var(--color-text-muted); font-size: var(--font-size-12); line-height: var(--line-height-normal); overflow-wrap: anywhere; }
.ptzAttentionFields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-3); }
.ptzAttentionField { min-width: 0; display: grid; gap: 6px; }
.ptzAttentionField > span { font-size: var(--font-size-13); font-weight: var(--font-weight-medium); }
.ptzAttentionField input, .ptzAttentionField select { width: 100%; min-width: 0; }
.ptzAttentionAdvanced { border: 1px solid var(--color-border-subtle); border-radius: var(--radius-control); padding: var(--space-3); }
.ptzAttentionAdvanced summary { cursor: pointer; font-weight: var(--font-weight-medium); }
.ptzAttentionAdvanced[open] summary { margin-bottom: var(--space-3); }
.ptzAttentionWizardFooter { display: flex; justify-content: space-between; gap: var(--space-3); margin-top: var(--space-5); }
.ptzAttentionWizardFooterGroup { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.ptzAttentionOperator { display: grid; gap: var(--space-3); }
.ptzAttentionInlineActions { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; }
.ptzAttentionInlineActions > * { min-width: 0; }
.ptzAttentionInlineActions .chipButton { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
@media (max-width: 900px) {
  .ptzAttentionLayout { grid-template-columns: minmax(0, 1fr); }
  .ptzAttentionProfileList { grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr)); }
}
@media (max-width: 640px) {
  .ptzAttentionHeader, .ptzAttentionHero { grid-template-columns: minmax(0, 1fr); display: grid; }
  .ptzAttentionHeaderActions, .ptzAttentionActions { justify-content: stretch; }
  .ptzAttentionHeaderActions > button, .ptzAttentionActions > button { flex: 1 1 150px; }
  .ptzAttentionTimeline { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ptzAttentionMetrics, .ptzAttentionFields { grid-template-columns: minmax(0, 1fr); }
  .ptzAttentionWizardSteps { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ptzAttentionWizardFooter { flex-direction: column-reverse; }
  .ptzAttentionWizardFooterGroup, .ptzAttentionWizardFooterGroup > button { width: 100%; }
  .ptzAttentionDecision { grid-template-columns: auto minmax(0, 1fr); }
  .ptzAttentionDecisionTime { grid-column: 2; }
}
@media (max-width: 420px) {
  .ptzAttentionCardBody { padding: var(--space-3); }
  .ptzAttentionTimeline, .ptzAttentionWizardSteps { gap: 6px; }
  .ptzAttentionStage, .ptzAttentionWizardStep { padding: var(--space-2); }
}
`;

export function installPtzAttentionStyles(): void {
  if (typeof document === "undefined" || document.getElementById(STYLE_ID)) return;
  const element = document.createElement("style");
  element.id = STYLE_ID;
  element.textContent = styles;
  document.head.appendChild(element);
}
