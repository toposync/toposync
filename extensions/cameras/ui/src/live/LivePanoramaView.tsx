import React, { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { resolveToposyncUrl } from '@toposync/plugin-api';
import type { LiveViewFrame, NavigableViewportController, ToposyncHost } from '@toposync/plugin-api';
import { createPanoramaVideoRenderer, frameStillSharesRegisteredView, type VideoGeometry } from './panoramaVideo';
import { FloatingVideoPanel } from './FloatingVideoPanel';
import { validPanoramaCrop } from '../settings/panoramaCrop';
import type { CameraSourcePanoramaArtifact } from '../types';
import { livePanoramaStyles } from './livePanorama';
const root = '/api/cameras/live-panorama';
async function request(path: string, method = 'GET', body?: unknown, signal?: AbortSignal) {
    const response = await fetch(resolveToposyncUrl(path), { method, credentials: 'same-origin', headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined, signal, keepalive: method === 'DELETE' });
    const result = await response.json();
    if (!response.ok)
        throw new Error(result?.detail?.message ?? result?.detail?.code ?? `Falha de conexão (${response.status})`);
    return result;
}
function api(path: string, method = 'GET', body?: unknown, signal?: AbortSignal) {
    return request(root + path, method, body, signal);
}
type Choice = {
    camera_id: string;
    camera_name: string;
    source_id: string;
    source_name: string;
    kind: string;
    artifact: CameraSourcePanoramaArtifact;
    reason: string | null;
    secondary_sources?: Array<{
        id: string;
        name: string;
    }>;
};
type Catalog = {
    choices: Choice[];
    selected: string;
    loading: boolean;
    error: string;
};
let catalog: Catalog = { choices: [], selected: '', loading: false, error: '' };
let catalogRequest: Promise<void> | null = null;
const catalogListeners = new Set<() => void>();
function selectableChoices(choices: Choice[]): Choice[] {
    const byCamera = new Map<string, Choice>();
    for (const item of choices) {
        if (item.reason)
            continue;
        const previous = byCamera.get(item.camera_id);
        if (!previous || previous.kind !== 'active' && item.kind === 'active')
            byCamera.set(item.camera_id, item);
    }
    return [...byCamera.values()].sort((left, right) => left.camera_name.localeCompare(right.camera_name));
}
function publishCatalog(next: Catalog) {
    catalog = next;
    catalogListeners.forEach(listener => listener());
}
function selectedArtifact(choices: Choice[]): string {
    const selectable = selectableChoices(choices);
    const saved = localStorage.getItem('toposync.livePanorama.reference');
    return selectable.find(item => item.artifact.id === saved)?.artifact.id
        ?? selectable[0]?.artifact.id
        ?? '';
}
async function loadCatalog(force = false): Promise<void> {
    if (catalogRequest && !force)
        return catalogRequest;
    publishCatalog({ ...catalog, loading: true, error: '' });
    catalogRequest = api('', 'GET').then(data => {
        const choices = data.choices as Choice[];
        const selectable = selectableChoices(choices);
        const selected = selectable.some(item => item.artifact.id === catalog.selected)
            ? catalog.selected
            : selectedArtifact(choices);
        publishCatalog({ choices, selected, loading: false, error: '' });
        if (selected)
            localStorage.setItem('toposync.livePanorama.reference', selected);
    }).catch(error => publishCatalog({ ...catalog, loading: false, error: String(error.message) })).finally(() => {
        catalogRequest = null;
    });
    return catalogRequest;
}
function chooseArtifact(id: string) {
    if (!selectableChoices(catalog.choices).some(item => item.artifact.id === id))
        return;
    localStorage.setItem('toposync.livePanorama.reference', id);
    publishCatalog({ ...catalog, selected: id, error: '' });
}
function useCatalog(): Catalog {
    const value = useSyncExternalStore(
        listener => { catalogListeners.add(listener); return () => catalogListeners.delete(listener); },
        () => catalog,
        () => catalog,
    );
    useEffect(() => { void loadCatalog(); }, []);
    return value;
}
type State = {
    can_control?: boolean;
    session_id: string;
    sequence: number;
    phase: string;
    error: string | null;
    moving: boolean;
    blocked: boolean;
    commands: number;
    result?: {
        sequence: number;
        verified: boolean;
    } | null;
};
const reasonLabel: Record<string, string> = { motion_automation_unqualified: 'Tracking e retorno automático precisam estar desativados e qualificados antes do apontamento.', external_automation_active: 'Uma automação da câmera está ativa. Apontamento suspenso.', source_panorama_changed: 'A referência mudou. Reabra a visualização antes de apontar.', panorama_visual_localization_failed: 'O vídeo atual não corresponde às referências com confiança suficiente. Apontamento indisponível.', panorama_visual_support_insufficient: 'Detalhes visuais insuficientes para localizar o vídeo.', panorama_visual_localization_ambiguous: 'Localização ambígua. Aguarde uma observação confiável.', panorama_source_geometry_changed: 'A geometria da fonte mudou. Reabra a visualização.', camera_control_permission_required: 'Sua permissão permite visualizar, mas não mover esta câmera.', stop_unconfirmed: 'Parada física não confirmada. Controle suspenso.', live_control_failed: 'Falha de controle. Verifique a câmera antes de tentar novamente.', visual_navigation_budget_exhausted: 'O limite de correções foi atingido sem confirmar a centralização.' };
const phaseLabel: Record<string, string> = { localizing: 'Localizando', unlocalized: 'Alinhamento não confirmado', aligned: 'Localizando', moving: 'Movendo', stopping: 'Parando', stabilizing: 'Estabilizando', error: 'Controle indisponível' };
function captureDate(value: unknown): string {
    const date = new Date(typeof value === 'number' ? value * 1000 : String(value));
    return Number.isFinite(date.getTime()) ? date.toLocaleString() : '';
}
/** Cheap per-presented-frame drift sample, independent of the pose estimator. */
function signature(image: CanvasImageSource, canvas: HTMLCanvasElement): Uint8ClampedArray {
    const context = canvas.getContext('2d', { willReadFrequently: true })!;
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    return context.getImageData(0, 0, canvas.width, canvas.height).data;
}
export function LivePanoramaView({ host }: {
    host: ToposyncHost;
}): React.ReactElement {
    const { choices, selected, error } = useCatalog();
    const refreshReferences = useCallback(() => { void loadCatalog(true); }, []);
    const choice = choices.find(item => item.artifact.id === selected);
    return <section className="livePanorama" aria-label="Panorâmica ao vivo">
    <style>{livePanoramaStyles}</style>
    {error && <p className="livePanoramaEmpty" role="alert">{error}</p>}
    {choice && !choice.reason ? <LiveSession key={`${choice.artifact.id}:${choice.artifact.revision}:${choice.artifact.crop_revision}`} host={host} choice={choice} refreshReferences={refreshReferences}/> : <p className="livePanoramaEmpty">Escolha uma câmera com panorama compatível no seletor superior.</p>}
  </section>;
}

export function LivePanoramaSelectorTrigger({ open }: { open: () => void }): React.ReactElement {
    const { choices, selected, loading } = useCatalog();
    const choice = choices.find(item => item.artifact.id === selected);
    const label = choice?.camera_name ?? (loading ? 'Carregando câmeras…' : 'Selecionar câmera');
    return <button className="chipButton mainSelectorButton" type="button" aria-label={`Câmera: ${label}`} title={`Câmera: ${label}`} onClick={open}>
      <span className="mainSelectorButtonText">{label}</span>
    </button>;
}

export function LivePanoramaSelectorContent({ close }: { close: () => void }): React.ReactElement {
    const { choices, selected, loading, error } = useCatalog();
    const selectable = selectableChoices(choices);
    return <div className="choiceList">
      {error ? <div className="errorText">{error}</div> : null}
      {loading && choices.length === 0 ? <div className="choiceItem" aria-disabled="true"><div className="choiceTitle">Carregando…</div></div> : null}
      {!loading && selectable.length === 0 ? <div className="choiceItem" aria-disabled="true"><div className="choiceTitle">Nenhuma câmera panorâmica disponível</div><div className="choiceDesc">Configure ou selecione um panorama compatível nas configurações da câmera.</div></div> : null}
      {selectable.map(item => {
        const select = () => { chooseArtifact(item.artifact.id); close(); };
        return <div key={item.camera_id} className={["choiceItem", selected === item.artifact.id ? "isSelected" : ""].filter(Boolean).join(" ")} role="button" tabIndex={0} onClick={select} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') select(); }}>
          <div className="choiceTitle">{item.camera_name}</div>
          <div className="choiceDesc">{item.source_name}{item.kind === 'candidate' ? ' · Panorama candidato' : ''}</div>
        </div>;
      })}
    </div>;
}
function LiveSession({ host, choice, refreshReferences }: {
    host: ToposyncHost;
    choice: Choice;
    refreshReferences: () => void;
}): React.ReactElement {
    const initialVisibility = typeof document === 'undefined' || document.visibilityState === 'visible';
    const [state, setState] = useState<State | null>(null), [error, setError] = useState(''), [aligned, setAligned] = useState(false), [connected, setConnected] = useState(false), [externalMoving, setExternalMoving] = useState(false), [visible, setVisible] = useState(initialVisibility), [telephotoMinimized, setTelephotoMinimized] = useState(false);
    const [marker, setMarker] = useState<{
        x: number;
        y: number;
    } | null>(null), [restart, setRestart] = useState(0);
    const stateRef = useRef<State | null>(null), sequence = useRef(0), alive = useRef(false), session = useRef('');
    const renderer = useRef<ReturnType<typeof createPanoramaVideoRenderer> | null>(null), canvas = useRef<HTMLCanvasElement | null>(null);
    const mask = useRef<ImageData | null>(null), navigation = useRef<NavigableViewportController | null>(null);
    const frame = useRef<LiveViewFrame | null>(null), registration = useRef<{
        geometry: VideoGeometry;
        signature: Uint8ClampedArray;
        epoch: string;
        verifiedAt: number;
    } | null>(null);
    const pendingTarget = useRef<{
        x: number;
        y: number;
        marker: { x: number; y: number };
        queuedAt: number;
    } | null>(null);
    const estimator = useRef(false), lastEstimate = useRef(0), lastFrame = useRef(0), epoch = useRef(''), externalMovingRef = useRef(false), visibleRef = useRef(initialVisibility), hiddenAt = useRef(0);
    const analysis = useRef(document.createElement('canvas')), probe = useRef(document.createElement('canvas'));
    const [bounds] = useState(() => savedBounds());
    const { width, height } = choice.artifact;
    // A second canonical copy lets seam-crossing saved crops remain contiguous.
    function savedBounds() { const c = choice.artifact.crop; return validPanoramaCrop(c) ? { x: c.u_start * choice.artifact.width, y: c.v_start * choice.artifact.height, width: c.u_width * choice.artifact.width, height: c.v_height * choice.artifact.height } : { x: 0, y: 0, width: choice.artifact.width, height: choice.artifact.height }; }
    function clear() { registration.current = null; renderer.current?.clear(); setAligned(false); }
    function accept(next: State) { if (!alive.current || next.session_id !== session.current || next.sequence < sequence.current)
        return; stateRef.current = next; setState(next); if (next.moving || next.blocked)
        clear(); }
    useEffect(() => {
        alive.current = true;
        let identifier = '';
        let disposed = false;
        const controller = new AbortController();
        probe.current.width = 160;
        probe.current.height = 90;
        void api('/sessions', 'POST', { camera_id: choice.camera_id, source_id: choice.source_id, artifact_id: choice.artifact.id, revision: choice.artifact.revision }).then(data => {
            identifier = data.session_id;
            if (disposed) {
                void api(`/sessions/${identifier}`, 'DELETE');
                return;
            }
            session.current = identifier;
            sequence.current = 0;
            accept(data);
        }).catch(e => { if (!disposed)
            setError(e.message); });
        const interval = setInterval(() => {
            if (!visibleRef.current)
                return;
            if (identifier)
                void api(`/sessions/${identifier}`, 'GET', undefined, controller.signal).then(accept).catch(e => { if (alive.current && !controller.signal.aborted) {
                    clear();
                    setError(e.message);
                } });
            if (performance.now() - lastFrame.current > 1500) {
                setConnected(false);
                clear();
                if (stateRef.current?.moving && stateRef.current.phase !== 'stopping')
                    stop();
            }
            if (pendingTarget.current && performance.now() - pendingTarget.current.queuedAt > 3000) {
                pendingTarget.current = null;
                setMarker(null);
                setError('O destino expirou antes de o vídeo ser localizado. Clique novamente.');
            }
        }, 750);
        return () => { disposed = true; alive.current = false; session.current = ''; pendingTarget.current = null; clearInterval(interval); controller.abort(); registration.current = null; renderer.current?.clear(); if (identifier)
            void api(`/sessions/${identifier}`, 'DELETE').catch(() => { }); };
    }, [restart]);
    useEffect(() => {
        const change = () => {
            const next = document.visibilityState === 'visible';
            visibleRef.current = next;
            setVisible(next);
            if (!next) {
                hiddenAt.current = performance.now();
                pendingTarget.current = null;
                setMarker(null);
                setConnected(false);
                clear();
                if (stateRef.current?.moving && stateRef.current.phase !== 'stopping')
                    stop();
                return;
            }
            if (hiddenAt.current && performance.now() - hiddenAt.current > 12000) {
                setError('Retomando a sessão após pausa em segundo plano.');
                setRestart(value => value + 1);
            }
            hiddenAt.current = 0;
        };
        document.addEventListener('visibilitychange', change);
        return () => document.removeEventListener('visibilitychange', change);
    }, []);
    useEffect(() => {
        if (!visible)
            return;
        const controller = new AbortController();
        let disposed = false;
        const inspect = () => {
            const query = new URLSearchParams({ source_id: choice.source_id, include_control: 'true' });
            void request(`/api/cameras/cameras/${encodeURIComponent(choice.camera_id)}/ptz/status?${query}`, 'GET', undefined, controller.signal).then(result => {
                if (disposed)
                    return;
                const physical = String(result?.status?.move_status ?? '').toUpperCase();
                const controlled = String(result?.control?.motion_state ?? '').toLowerCase();
                const moving = physical.includes('MOVING') || controlled === 'moving';
                const external = moving && !stateRef.current?.moving;
                if (external !== externalMovingRef.current) {
                    externalMovingRef.current = external;
                    setExternalMoving(external);
                    clear();
                    if (external) {
                        pendingTarget.current = null;
                        setMarker(null);
                        setError('Movimento externo detectado. Aguardando estabilização para relocalizar.');
                    }
                }
            }).catch(() => { /* Visual localization remains the fallback when PTZ status is unavailable. */ });
        };
        inspect();
        const interval = window.setInterval(inspect, 1500);
        return () => { disposed = true; controller.abort(); window.clearInterval(interval); externalMovingRef.current = false; };
    }, [choice.camera_id, choice.source_id, restart, visible]);
    useEffect(() => {
        let cancelled = false;
        const image = new Image();
        image.src = resolveToposyncUrl(choice.artifact.coverage_url ?? "");
        image.onload = () => {
            if (cancelled || !canvas.current)
                return;
            const scratch = document.createElement('canvas');
            scratch.width = width;
            scratch.height = height;
            const context = scratch.getContext('2d')!;
            context.drawImage(image, 0, 0);
            mask.current = context.getImageData(0, 0, width, height);
            try {
                renderer.current = createPanoramaVideoRenderer(canvas.current, image, 2);
            }
            catch (e) {
                setError(String(e));
            }
        };
        image.onerror = () => setError('Máscara de cobertura indisponível.');
        return () => { cancelled = true; renderer.current?.dispose(); renderer.current = null; mask.current = null; };
    }, [choice.artifact.id]);
    const onFrame = useCallback((value: LiveViewFrame | null) => {
        if (!alive.current)
            return;
        if (!visibleRef.current) {
            frame.current = null;
            clear();
            return;
        }
        if (!value || value.sourceId !== choice.source_id || value.cameraId !== choice.camera_id) {
            frame.current = null;
            clear();
            if (stateRef.current?.moving && stateRef.current.phase !== 'stopping')
                stop();
            return;
        }
        frame.current = value;
        lastFrame.current = performance.now();
        setConnected(true);
        if (externalMovingRef.current) {
            clear();
            return;
        }
        if (epoch.current !== value.epoch) {
            epoch.current = value.epoch;
            clear();
            if (stateRef.current?.moving && stateRef.current.phase !== 'stopping')
                stop();
        }
        let currentSignature: Uint8ClampedArray;
        try {
            currentSignature = signature(value.image, probe.current);
        }
        catch {
            clear();
            setError('Este transporte não permite ler os frames autenticados para localização.');
            return;
        }
        const observed = registration.current;
        if (observed && observed.epoch === value.epoch && performance.now() - observed.verifiedAt < 2500 && !stateRef.current?.moving && !stateRef.current?.blocked && frameStillSharesRegisteredView(observed.signature, currentSignature, probe.current.width, probe.current.height)) {
            try {
                renderer.current?.draw(value.image, observed.geometry);
                setAligned(!!renderer.current);
            }
            catch (e) {
                clear();
                setError(String(e));
            }
        }
        else if (observed) {
            clear();
        }
        if (!value.opticalSourceSize || !value.contentRect) {
            clear();
            setError('Geometria do transporte ainda não confirmada.');
            return;
        }
        if (!session.current || estimator.current || stateRef.current?.moving || performance.now() - lastEstimate.current < 800)
            return;
        estimator.current = true;
        lastEstimate.current = performance.now();
        const identifier = session.current, intentionSequence = sequence.current, submitted = value;
        const sourceSize = value.opticalSourceSize, rect = value.contentRect;
        const sample = analysis.current;
        sample.width = Math.min(960, sourceSize.width);
        sample.height = Math.round(sample.width * sourceSize.height / sourceSize.width);
        sample.getContext('2d')!.drawImage(value.image, rect.x * value.width, rect.y * value.height, rect.width * value.width, rect.height * value.height, 0, 0, sample.width, sample.height);
        const started = performance.now();
        void api(`/sessions/${identifier}/observe`, 'POST', { sequence: value.sequence, epoch: value.epoch, media_time: value.mediaTime, width: sourceSize.width, height: sourceSize.height, image: sample.toDataURL('image/jpeg', .86).split(',')[1] }).then(result => {
            if (!alive.current || !visibleRef.current || session.current !== identifier || sequence.current !== intentionSequence || epoch.current !== submitted.epoch)
                return;
            if (result.status === 'localized' && result.geometry && performance.now() - started < 1500 && !stateRef.current?.moving) {
                registration.current = { geometry: { ...result.geometry, content_rect: rect }, signature: currentSignature, epoch: submitted.epoch, verifiedAt: performance.now() };
                setError('');
                const queued = pendingTarget.current;
                if (queued && performance.now() - queued.queuedAt <= 3000)
                    dispatchIntent(queued);
            }
            else {
                clear();
                if (result.reason)
                    setError(result.reason);
            }
            accept(result);
        }).catch(e => { if (alive.current && session.current === identifier) {
            clear();
            setError(e.message);
        } }).finally(() => { estimator.current = false; });
    }, [choice.camera_id, choice.source_id]);
    function click(point: {
        x: number;
        y: number;
    }) {
        const current = stateRef.current;
        if (!current) {
            setError('O controle ainda está iniciando.');
            return;
        }
        if (current.can_control === false) {
            setError(reasonLabel.camera_control_permission_required);
            return;
        }
        if (current.blocked) {
            setError('O estado físico da câmera precisa ser requalificado antes de outro apontamento.');
            return;
        }
        if (!connected) {
            setError('Aguarde a transmissão atual antes de apontar a câmera.');
            return;
        }
        const x = ((point.x / width) % 1 + 1) % 1, y = point.y / height;
        if (y < 0 || y > 1 || point.x < 0 || point.x > 2 * width) {
            setError('O destino está fora do panorama navegável.');
            return;
        }
        const coverage = mask.current;
        if (!coverage || coverage.data[(Math.min(height - 1, Math.floor(y * height)) * width + Math.min(width - 1, Math.floor(x * width))) * 4] === 0) {
            setError('Ponto fora da cobertura capturada.');
            return;
        }
        const target = { x, y, marker: { x: point.x, y: point.y }, queuedAt: performance.now() };
        setMarker(target.marker);
        if (!aligned && !current.moving) {
            pendingTarget.current = target;
            setError('Destino aguardando uma localização atual do vídeo.');
            return;
        }
        dispatchIntent(target);
    }
    function dispatchIntent(target: { x: number; y: number; marker: { x: number; y: number }; queuedAt: number }) {
        if (!session.current || !stateRef.current)
            return;
        pendingTarget.current = null;
        setMarker(target.marker);
        clear();
        const next = ++sequence.current;
        stateRef.current = { ...stateRef.current, moving: true, sequence: next, phase: 'moving' };
        setState(stateRef.current);
        void api(`/sessions/${session.current}/intent`, 'POST', { sequence: next, x: target.x, y: target.y }).then(accept).catch(e => { if (alive.current && sequence.current === next) {
            stateRef.current = { ...stateRef.current!, moving: false, phase: 'error' };
            setState(stateRef.current);
            setError(e.message);
        } });
    }
    function stop() { pendingTarget.current = null; clear(); setMarker(null); if (stateRef.current) {
        stateRef.current = { ...stateRef.current, phase: 'stopping' };
        setState(stateRef.current);
    } const next = ++sequence.current; void api(`/sessions/${session.current}/stop`, 'POST', { sequence: next }).then(accept).catch(e => setError(e.message)); }
    const label = !connected ? 'Aguardando vídeo atual' : externalMoving ? 'Câmera em movimento externo' : aligned ? 'Vídeo alinhado' : phaseLabel[state?.phase ?? 'localizing'] ?? 'Alinhamento não confirmado';
    return <div style={{ position: 'relative', flex: 1, minHeight: 360, overflow: 'hidden' }}>
    <host.ui.NavigableViewport label="Panorama navegável" contentKey={choice.artifact.id} contentSize={{ width: width * 2, height }} initialBounds={bounds} controllerRef={navigation} onContentClick={click} style={{ width: '100%', height: '100%', minHeight: 360, background: '#151920' }}>
      <img draggable={false} src={resolveToposyncUrl(choice.artifact.image_url)} alt="Panorama capturado — referência histórica" style={{ position: 'absolute', width, height, filter: 'grayscale(1) brightness(.65)', pointerEvents: 'none' }}/>
      <img draggable={false} src={resolveToposyncUrl(choice.artifact.image_url)} alt="" style={{ position: 'absolute', left: width, width, height, filter: 'grayscale(1) brightness(.65)', pointerEvents: 'none' }}/>
      <canvas ref={canvas} width={width * 2} height={height} style={{ position: 'absolute', width: width * 2, height, pointerEvents: 'none' }}/>
      {marker && <div aria-label="Destino desejado" style={{ position: 'absolute', left: marker.x, top: marker.y, transform: 'translate(-50%,-50%)', width: 24, height: 24, border: '2px solid #f6c95d', borderRadius: '50%', pointerEvents: 'none', boxShadow: '0 0 0 1px black' }}/>}
    </host.ui.NavigableViewport>
    <div className="livePanoramaControls">
      <span className="livePanoramaStatus" role="status" data-aligned={aligned}>{label}{aligned && state?.result?.verified && state.result.sequence === sequence.current ? ' · Centro confirmado' : ''}</span>
      <button className="iconButton" type="button" onClick={() => navigation.current?.zoomBy(1.4)} aria-label="Ampliar visualização" title="Ampliar visualização"><i className="fa-solid fa-plus" aria-hidden="true" /></button>
      <button className="iconButton" type="button" onClick={() => navigation.current?.zoomBy(1 / 1.4)} aria-label="Reduzir visualização" title="Reduzir visualização"><i className="fa-solid fa-minus" aria-hidden="true" /></button>
      {state?.moving ? <button className="iconButton iconButtonDanger livePanoramaStop" type="button" onClick={stop} aria-label="Parar movimento" title="Parar movimento"><i className="fa-solid fa-stop" aria-hidden="true" /></button> : null}
    </div>
    <FloatingVideoPanel title={`Vídeo atual · ${label}`} hidden={aligned} initialLeft={16} initialWidth={360}>
      {host.ui.LiveViewPlayer ? <host.ui.LiveViewPlayer cameraId={choice.camera_id} sourceId={choice.source_id} active={visible} controls={false} context="large" onFrame={onFrame} style={{ height: '100%' }}/> : null}
    </FloatingVideoPanel>
    {choice.secondary_sources?.length && host.ui.LiveViewPlayer ? <FloatingVideoPanel title={`Teleobjetiva · ${choice.secondary_sources[0].name}`} initialLeft={396} minimizable onMinimizedChange={setTelephotoMinimized}>
      <host.ui.LiveViewPlayer cameraId={choice.camera_id} sourceId={choice.secondary_sources[0].id} active={visible && !telephotoMinimized} controls={false} context="pip" style={{ height: '100%' }}/>
    </FloatingVideoPanel> : null}
    <div className="livePanoramaFooter">
      <span>Panorama capturado{choice.artifact.created_at ? ' em ' + captureDate(choice.artifact.created_at) : ''} · {choice.kind === 'candidate' ? 'Candidato selecionado' : 'Referência ativa'} · revisão {choice.artifact.revision}</span>
      {(error || state?.error) && <span className="livePanoramaAlert" role="alert">{reasonLabel[error || state?.error || ''] ?? (error || state?.error)}</span>}
      <button className="chipButton livePanoramaReconnect" type="button" onClick={() => { setError(''); setState(null); stateRef.current = null; setRestart(value => value + 1); refreshReferences(); }}>Reconectar</button>
    </div>
  </div>;
}
