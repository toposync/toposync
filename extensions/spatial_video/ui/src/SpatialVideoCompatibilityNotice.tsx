import React, { useState } from "react";
import type { HostI18n } from "@toposync/plugin-api";
import type { PanoramaProjectionIssue } from "./panoramaGroundLayer";

type Props = {
  loading: boolean;
  error: string | null;
  issues: PanoramaProjectionIssue[];
  hasCompatibleProjection: boolean;
  i18n: HostI18n;
  onConfigure?: (elementId?: string, initialAction?: string) => void;
};

export function SpatialVideoCompatibilityNotice({ loading, error, issues, hasCompatibleProjection, i18n, onConfigure }: Props): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const [expanded, setExpanded] = useState(false);
  if (!loading && !error && !issues.length && hasCompatibleProjection) return null;
  const partial = hasCompatibleProjection;
  const title = t(`ext.spatial_video.notice_${loading ? "loading" : partial ? "partial" : error || issues.length ? "failed" : "empty"}`);
  return <div role={loading ? "status" : "alert"} className={`spatialProjectionNotice${partial ? " isPartial" : ""}`}>
    <div className="spatialProjectionNoticeBody" onPointerDown={event => event.stopPropagation()} onWheel={event => event.stopPropagation()}>
      <strong><i className={`fa-solid fa-${loading ? "spinner" : "circle-info"}`} aria-hidden="true" /> {title}</strong>
      {!loading && <p>{t(partial ? "ext.spatial_video.notice_partial_help" : "ext.spatial_video.notice_empty_help")}</p>}
      {issues.length > 0 && <details open={expanded || !partial} onToggle={event => setExpanded(event.currentTarget.open)}><summary>{t("ext.spatial_video.affected", { count: issues.length })}</summary><ul>
        {issues.map(issue => <li key={issue.elementId}><strong>{issue.name}</strong><p>{issue.message}</p>
          {onConfigure && <button type="button" className="chipButton" onClick={() => onConfigure(issue.elementId, "panorama-mapping")}>{t("ext.spatial_video.review_mapping")}</button>}
        </li>)}
      </ul></details>}
      {error && <details><summary>{t("ext.spatial_video.stream_error")}</summary><p>{error}</p></details>}
      {!loading && !partial && !issues.length && onConfigure && <button type="button" className="chipButton" onClick={() => onConfigure()}>{t("ext.spatial_video.configure")}</button>}
    </div>
  </div>;
}
