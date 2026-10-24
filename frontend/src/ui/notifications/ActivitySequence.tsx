import React, { useEffect, useMemo, useRef, useState } from "react";
import { activityUrl, getActivityImages, type ActivityImagePage } from "../../util/activity";
import { i18n } from "../../util/i18n";
import { imageAt, imageAvailable, publishPlayback, sameImageSource } from "./activityPlayback";

export function ActivitySequence({ identifier, participant, observedEnd, clockDomain }: { identifier: string; participant: string; observedEnd?: number; clockDomain?: string }) {
  const { t } = i18n.useI18n();
  const [after, setAfter] = useState(0);
  const [previous, setPrevious] = useState<number[]>([]);
  const [page, setPage] = useState<ActivityImagePage | null>(null);
  const [error, setError] = useState(false);
  const [playing, setPlayingState] = useState(false);
  const playingNow = useRef(false);
  const setPlaying = (value: boolean) => { playingNow.current = value; setPlayingState(value); };
  const [time, setTime] = useState(0);
  const [urls, setUrls] = useState<Array<{ id: string; url: string; kind: string }>>([]);
  const [scope, setScope] = useState("");
  useEffect(() => { setAfter(0); setPrevious([]); setPage(null); setPlaying(false); }, [identifier, participant]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setPage(null); setError(false); setPlaying(false); publishPlayback(null);
    const load = () => {
      if (document.hidden) return;
      void getActivityImages(identifier, after, controller.signal).then(value => {
        if (!controller.signal.aborted) { setPage(value); timer = setTimeout(load, 5000); }
      }).catch(() => { if (!controller.signal.aborted) { setPage(null); setError(true); publishPlayback(null); } });
    };
    const hide = () => { if (document.hidden) { setPlaying(false); setPage(null); publishPlayback(null); } else load(); };
    load(); document.addEventListener("visibilitychange", hide);
    return () => { controller.abort(); clearTimeout(timer); publishPlayback(null); document.removeEventListener("visibilitychange", hide); };
  }, [identifier, after, participant]);
  const images = useMemo(() => (page?.images || []).filter(item => !participant || item.participant_id === participant), [page, participant]);
  const scopes = useMemo(() => images.filter((item, index) => !images.slice(0, index).some(other => sameImageSource(item, other))), [images]);
  const selectedScope = scopes.find(item => item.id === scope) || scopes[0];
  const frames = useMemo(() => images.filter(item => selectedScope && sameImageSource(item, selectedScope)).sort((a, b) => a.observed_at - b.observed_at), [images, selectedScope]);
  const start = frames[0]?.observed_at || 0, captureEnd = frames.at(-1)?.observed_at || start;
  const end = observedEnd !== undefined && clockDomain === selectedScope?.clock_domain
    ? Math.max(captureEnd, Math.min(observedEnd, captureEnd + 1 / (frames.at(-1)?.rate || 1))) : captureEnd;
  const frameCount = new Set(frames.map(item => item.frame_sequence)).size;
  useEffect(() => { setTime(start); setPlaying(false); }, [start, after, scope]);
  useEffect(() => {
    if (!playing) return;
    let previousTime = performance.now();
    const timer = setInterval(() => {
      if (!playingNow.current) return;
      const now = performance.now(), elapsed = (now - previousTime) / 1000;
      previousTime = now;
      setTime(value => { if (!playingNow.current) return value; if (value + elapsed >= end) { setPlaying(false); return end; } return value + elapsed; });
    }, 50);
    return () => clearInterval(timer);
  }, [playing, end]);
  const current = imageAt(frames, time);
  const currentImages = current ? frames.filter(item => item.frame_sequence === current.frame_sequence && item.observed_at === current.observed_at && imageAvailable(item)) : [];
  const selectionKey = currentImages.map(item => `${item.id}:${item.expires_at}`).join(",");
  useEffect(() => {
    setUrls([]); publishPlayback(null);
    if (!current?.image_href) return;
    const controller = new AbortController();
    const objectUrls: string[] = [];
    void Promise.all(currentImages.map(item => fetch(activityUrl(identifier, `/images/${encodeURIComponent(item.id)}`), { signal: controller.signal, cache: "no-store" }).then(response => {
      if (!response.ok) throw new Error("Image unavailable");
      return response.blob();
    }).then(blob => {
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob); objectUrls.push(url);
      return { image: item, url, trail: frames.filter(frame => frame.participant_id === item.participant_id && frame.observed_at <= item.observed_at) };
    }))).then(samples => {
      if (controller.signal.aborted) return;
      const valid = samples.filter((sample): sample is NonNullable<typeof sample> => !!sample);
      setUrls(valid.map(sample => ({ id: sample.image.id, url: sample.url, kind: sample.image.kind })));
      if (valid.length) publishPlayback({ occurrence: identifier, ...valid[0], companions: valid.slice(1) });
    }).catch(() => { if (!controller.signal.aborted) { setError(true); setUrls([]); publishPlayback(null); } });
    const expiry = setTimeout(() => { setUrls([]); publishPlayback(null); }, Math.max(0, Math.min(...currentImages.map(item => item.expires_at)) * 1000 - Date.now()));
    return () => { controller.abort(); clearTimeout(expiry); objectUrls.forEach(url => URL.revokeObjectURL(url)); publishPlayback(null); };
  }, [identifier, selectionKey]);
  if (!page && !error) return <p role="status">{t("activity.loading")}</p>;
  if (error) return <p role="alert">{t("activity.unavailable")}</p>;
  if (!images.length && !after && !page?.next_cursor) return null;
  return <section className="activitySequence" aria-label={t("activity.sequence")}>
    <h4>{t("activity.sequence")}</h4>
    <p>{t("activity.image_rate", { count: frameCount, rate: captureEnd > start ? ((frameCount - 1) / (captureEnd - start)).toFixed(2) : "—" })}</p>
    {scopes.length > 1 && <label>{t("activity.source_select")}<select className="input" value={selectedScope?.id} onChange={event => setScope(event.target.value)}>{scopes.map(item => <option key={item.id} value={item.id}>{item.camera_id} · {item.source_id} · {item.clock_domain}</option>)}</select></label>}
    <div className="activitySequenceFrame">{urls.length ? urls.map(item => <img key={item.id} src={item.url} alt={t(item.kind === "segmentation" ? "activity.segmented" : "activity.cropped")} />) : <span role="status">{t("activity.gap")}</span>}</div>
    <div className="activitySequenceControls"><button className="chipButton" type="button" disabled={frames.length < 2} aria-pressed={playing} onClick={() => { if (time >= end) setTime(start); setPlaying(!playing); }}>{t(playing ? "activity.pause" : "activity.play")}</button>
      <input aria-label={t("activity.sequence_time")} type="range" min={start} max={end || start + 1} step="any" value={Math.max(start, Math.min(end, time))} disabled={frames.length < 2} onChange={event => { setPlaying(false); setTime(Number(event.target.value)); }} />
      <output>{(time - start).toFixed(2)} / {(end - start).toFixed(2)} s</output></div>
    <p>{current ? t(current.kind === "segmentation" ? "activity.segmented" : "activity.cropped") : t("activity.gap")}{current?.members?.length ? ` · ${current.members.length}` : ""}</p>
    <div className="activitySequenceControls"><button className="chipButton" type="button" disabled={!previous.length} onClick={() => { setAfter(previous.at(-1)!); setPrevious(previous.slice(0, -1)); }}>{t("activity.previous")}</button><button className="chipButton" type="button" disabled={!page?.next_cursor} onClick={() => { setPrevious([...previous, after]); setAfter(page!.next_cursor!); }}>{t("activity.next")}</button></div>
  </section>;
}
