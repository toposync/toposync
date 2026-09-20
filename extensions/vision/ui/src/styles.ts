export const styles = `
.identityGallery {display:grid;gap:16px;min-width:0;color:var(--text)}
.identityGallery h3,.identityGallery p {margin:0}.identityGallery p {line-height:1.5}
.identityGallery .identityMuted {color:var(--muted);font-size:13px}
.identityGallery .identityToolbar {display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.identityGallery .identityToolbar input:not([type=checkbox]):not([type=radio]) {flex:1;min-width:150px}
.identityGallery input:not([type=checkbox]):not([type=radio]),.identityGallery select {box-sizing:border-box;width:100%;min-height:44px;border:1px solid var(--borderStrong);border-radius:8px;padding:8px 12px;background:var(--panel);color:var(--text);font:inherit}
.identityGallery button {min-height:44px;white-space:normal;overflow-wrap:anywhere}
.identityGallery fieldset {border:0;padding:0;margin:0;display:grid;gap:14px;min-width:0}
.identityGallery label {display:flex;align-items:center;justify-content:flex-start;gap:8px;line-height:1.5}
.identityGallery .identityField {display:grid;gap:6px}
.identityGallery input[type=checkbox],.identityGallery input[type=radio] {width:20px;height:20px;flex-shrink:0;accent-color:var(--accent)}
.identityGallery :focus-visible {outline:2px solid var(--accent);outline-offset:3px}
.identityGallery .identityGrid {display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:12px}
.identityGallery .identityTile {border:1px solid var(--border);border-radius:10px;padding:10px;display:grid;gap:8px;text-align:left;background:var(--panel);color:var(--text);min-width:0}
.identityGallery .identityTile:has(> .identityGroupDetails[open]) {grid-column:1 / -1}
.identityGallery .identityTile:has(> .identityGroupDetails[open]) > .identityPhotoButton {max-width:160px}
.identityGallery .identityGroupDetails summary {min-height:44px;cursor:pointer;line-height:1.5;overflow-wrap:anywhere}
.identityGallery .identityGroupDetails > p {margin-bottom:12px}
.identityGallery .identityGroupDetails label {min-height:44px}
.identityGallery .identityTile.isSelected {border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.identityGallery .identityPhoto {width:100%;height:112px;object-fit:cover;border-radius:6px;background:var(--bg)}
.identityGallery .identityPortrait {width:72px;height:72px;border-radius:12px;object-fit:cover}
.identityGallery .identityPhotoButton {padding:0;border:0;background:transparent;color:inherit;cursor:zoom-in;border-radius:8px;min-width:0;text-align:left}
.identityGallery .identityFullPhoto {display:block;width:100%;max-height:65vh;object-fit:contain;border-radius:8px;background:var(--bg)}
.identityGallery .identityInspector {display:grid;gap:12px;padding:12px;border:1px solid var(--borderStrong);border-radius:12px;min-width:0}
.identityGallery .identityInspector .identityToolbar {justify-content:space-between}
.identityGallery .identityForm {padding:16px;border:1px solid var(--borderStrong);border-radius:12px;display:grid;gap:16px;background:var(--panel)}
.identityGallery .identityError {border-left:3px solid var(--color-error,#d85656);padding:10px}
.identityGallery .identityActions {display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end}
.identityGallery .identityName {overflow-wrap:anywhere;font-weight:600}
.identityGallery .identityHistory {display:flex;gap:12px;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border)}
.identityGallery .identityChoices {display:grid;gap:8px;max-height:280px;overflow:auto;padding:3px}
.identityGallery .identityChoice {border:1px solid var(--border);border-radius:8px;padding:8px;min-height:48px;cursor:pointer}
.identityGallery .identityChoice.isSelected {border-color:var(--accent)}
.identityGallery .identityChoice .identityPortrait {width:48px;height:48px}
@media(max-width:480px){.identityGallery .identityGrid{grid-template-columns:repeat(2,minmax(0,1fr))}.identityGallery .identityActions button{flex:1}.identityGallery .identityForm{padding:12px}}
`;
