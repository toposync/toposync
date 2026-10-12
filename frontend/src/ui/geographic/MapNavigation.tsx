import React, { useEffect, useRef, useState } from "react";
import * as L from "leaflet";
import { searchGeographicPlaces, type GeographicMapConfiguration, type GeographicPlace } from "../../util/api";
import { i18n } from "../../util/i18n";
import { useMapConsent } from "./mapConsent";

export function MapNavigation({ map, configuration, onHome }: {
  map: L.Map; configuration: GeographicMapConfiguration; onHome: () => void;
}): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const consent = useMapConsent(`search:${configuration.geocoding.host}`);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<GeographicPlace[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [permissionOpen, setPermissionOpen] = useState(false);
  const [locating, setLocating] = useState(false);
  const [message, setMessage] = useState("");
  const search = useRef<AbortController | null>(null);
  const locationGeneration = useRef(0);
  const destination = useRef<L.CircleMarker | null>(null);
  useEffect(() => () => { search.current?.abort(); locationGeneration.current++; destination.current?.remove(); }, []);
  useEffect(() => {
    if (consent.decision !== true) { search.current?.abort(); setSearching(false); setResults(null); }
  }, [consent.decision]);
  function showDestination(latitude: number, longitude: number): void {
    destination.current?.remove();
    destination.current = L.circleMarker([latitude, longitude], { radius: 7, color: "#166534", fillOpacity: 0.7 }).addTo(map);
    map.setView([latitude, longitude], Math.max(17, map.getZoom()));
  }
  async function findPlaces(): Promise<void> {
    if (!query.trim()) return;
    search.current?.abort();
    const controller = new AbortController();
    search.current = controller;
    setSearching(true); setMessage(""); setResults(null);
    try {
      const found = await searchGeographicPlaces(query.trim(), locale, controller.signal);
      if (!controller.signal.aborted) setResults(found);
    } catch (error) {
      if (!controller.signal.aborted) setMessage((error as { status?: number }).status === 429 ? "core.map.search_rate_limit" : "core.map.search_failed");
    } finally { if (!controller.signal.aborted) setSearching(false); }
  }
  function locate(): void {
    if (!window.isSecureContext || !navigator.geolocation) { setMessage("core.map.location_unavailable"); return; }
    const generation = ++locationGeneration.current;
    setLocating(true); setMessage("");
    navigator.geolocation.getCurrentPosition(position => {
      if (generation !== locationGeneration.current) return;
      setLocating(false);
      if (![position.coords.latitude, position.coords.longitude, position.coords.accuracy].every(Number.isFinite)
        || Math.abs(position.coords.latitude) > 85 || Math.abs(position.coords.longitude) > 180) { setMessage("core.map.location_unavailable"); return; }
      showDestination(position.coords.latitude, position.coords.longitude);
      setMessage(t("core.map.location_accuracy", { metres: Math.ceil(position.coords.accuracy) }));
    }, error => {
      if (generation !== locationGeneration.current) return;
      setLocating(false); setMessage(error.code === 1 ? "core.map.location_denied" : error.code === 3 ? "core.map.location_timeout" : "core.map.location_unavailable");
    }, { timeout: 10000, maximumAge: 30000, enableHighAccuracy: true });
  }
  return <div className="geographicNavigation">
    <form className="geographicSearch" onSubmit={event => {
      event.preventDefault();
      if (consent.decision === true) void findPlaces(); else if (query.trim()) setPermissionOpen(true);
    }}>
      <label className="field geographicSearchField"><span>{t("core.map.address")}</span>
        <input className="input" type="search" maxLength={240} value={query} onChange={event => {
          search.current?.abort(); setSearching(false); setResults(null); setMessage(""); setQuery(event.target.value);
        }} /></label>
      <button className="chipButton" type="submit" disabled={searching || !query.trim()}>{t(searching ? "core.map.searching" : "core.map.search")}</button>
      <button className="chipButton" type="button" disabled={locating} onClick={locate}>{t(locating ? "core.map.locating" : "core.map.my_location")}</button>
      <button className="chipButton" type="button" onClick={() => { destination.current?.remove(); destination.current = null; onHome(); }}>{t("core.map.home")}</button>
    </form>
    {permissionOpen && <div className="geographicPermission" role="alert">
      <p>{t("core.map.search_consent", { provider: configuration.geocoding.name, host: configuration.geocoding.host })}</p>
      <button className="primaryButton" type="button" onClick={() => { consent.choose(true); setPermissionOpen(false); void findPlaces(); }}>{t("core.map.allow_search")}</button>
      <button className="chipButton" type="button" onClick={() => { consent.choose(false); setPermissionOpen(false); }}>{t("core.actions.cancel")}</button>
    </div>}
    {results?.length === 0 && <p role="status">{t("core.map.no_results")}</p>}
    {results && results.length > 0 && <ul className="geographicResults" aria-label={t("core.map.search_results")}>
      {results.map(place => <li key={place.id}><button className="chipButton" type="button" onClick={() => {
        showDestination(place.latitude, place.longitude); setResults(null);
      }}>{place.label}</button></li>)}
    </ul>}
    {message && <p role="status">{t(message)}</p>}
    {!consent.persistent && <p role="status">{t("core.map.permission_session")}</p>}
    {consent.decision === true && <button className="geographicServiceButton" type="button" onClick={() => { consent.choose(false); search.current?.abort(); }}>{t("core.map.revoke_search")}</button>}
  </div>;
}
