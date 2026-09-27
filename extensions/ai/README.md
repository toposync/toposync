# Toposync AI Extension

First-party extension for AI-assisted pipeline operators.

This first phase focuses on image workflows:

- `ai.smart_crop`: locates an object/region from a natural-language description and crops the frame.
- `ai.condition_filter`: evaluates a natural-language visual condition and emits packets only when it matches.

The extension is designed around local-first usage. Ollama is the default provider target, with
`qwen3.8:27b-mlx` as the initial high-quality local vision recommendation. Cloud providers can be
added through provider profiles and explicit fallback chains.

## Visual occurrences

`ai.condition_filter` supports `emit_mode: "occurrence"`. Each new captured frame is evaluated;
filter decision caching and permissive failure policies do not apply in this mode. A match lasting
`minimum_duration_seconds` opens an event, later matches update it, and a negative, missing image,
unverifiable capture freshness or source close ends it. Silence expires after
`evidence_timeout_seconds`. Duration ends at the last confirmed observation; an uncertain ending
does not certify that the physical activity stopped. Capture publication age does not certify
camera buffering or remote clock synchronization. Evaluation is serialized to preserve event order.

Occurrence mode requires the existing `capture_evidence` contract and monotonic media timestamps.
Repeated captures and out-of-order observations do not extend duration. The event carries a stable
subject and correlation identifier, the last evidence packet identifier, image, model result and
`condition_occurrence` timing/status. A subsequent visit receives a different identifier.
Capture instance/generation changes close the old occurrence and allow a restarted media clock.
Old generations and publications older than the accepted capture are ignored. Publication ordering
therefore requires a coherent source clock; clock regression does not certify continuity.
State is bounded by `maximum_occurrence_streams` (128 by default); least recently used streams
are closed with `stream_capacity` before eviction. Replay protection is scoped to retained runtime
state, while notification occurrence identifiers remain deterministic for the same capture epoch.

Camera preset requests accept `cat_litter_use` and `cat_drinking`. These recipes combine a fresh
semantic crop, explicit visual condition, occurrence lifecycle, stored image and occurrence-scoped
notification deduplication. The litter recipe reports observable box activity, not elimination;
the water recipe requires visible drinking action, not proximity. Neither estimates health,
identity or consumed volume. They require a configured vision-capable AI profile and must be
validated on representative positive, difficult negative and occluded recordings before relying
on their recognition. API persistence and synthetic replay tests do not establish model accuracy.

## History time windows

`GET /api/notifications`, the `notifications.list` service and
`GET /api/ai/history/search` accept optional `created_after` (inclusive) and
`created_before` (exclusive) as finite, nonnegative Unix seconds. Both bounds
filter notification creation time before pagination or the semantic corpus limit.
Updating an existing notification does not move it into a newer creation window.
The semantic route also applies the window during its consistency read and literal
fallback; its result reports `time_basis: "notification_created_at"` and the bounds.

Callers must convert civil dates using an explicit time zone and each local
midnight separately, since a daylight-saving day may differ from 24 hours. These
parameters do not interpret natural-language dates or certify the physical capture
timestamp. Configure the installation time zone under **Settings → Base**; it is
stored as `settings.core.timezone` and remains unset until selected explicitly.
`PATCH /api/settings/timezone` saves only that validated IANA zone under the existing
settings write permission, preserving other settings. Send `{"timezone": null}` to
clear it.

With no explicit numeric bounds, both history endpoints recognize a finite date
suffix: `hoje`/`ontem`, `today`/`yesterday`, optionally followed by morning (06–12),
afternoon (12–18), evening/night (18–24), or Portuguese `de madrugada` (00–06).
For example, `gato no bebedouro ontem à tarde` selects yesterday's 12–18 interval
in the installation zone, then searches the remaining terms. The interface shows
the exact half-open interval and its notification-creation basis. A query containing
only the date filters history without invoking an embedding model. Missing zone
returns a configuration request and no results; unsupported expressions remain
literal text. This is not unrestricted natural-language date understanding.
Refresh the search to include new events or recalculate a relative day; live
updates preserve the current server-selected results, including pagination.
The semantic limit remains
250 recent notifications within the selected interval, with resumable indexing
in batches of 32; the result reports partial coverage while indexing remains.

## Assistente de receitas e alteração de pipelines

**Criar receita** interpreta o pedido usando o catálogo atual de câmeras/fontes de vídeo
habilitadas, áreas mapeadas e receitas existentes. O modelo seleciona recursos; o construtor
da extensão de câmeras produz e compila o grafo. Ambiguidades retornam uma pergunta.
A prévia não grava; salvar revalida os recursos e cria desativado. Desfazer remove somente
a receita ainda idêntica à criada, com comparação protegida pelo bloqueio do armazenamento.
Não executa nem ativa dispositivos. Receitas visuais de gatos usam os perfis de análise
configurados, que devem ser revisados antes da ativação.

O painel **Assistente de pipelines** prepara uma proposta para um pipeline existente,
desativado, em modo JSON ou interativo. A proposta usa um perfil Ollama habilitado
em endereço local, passa pelo compilador real e mostra as alterações antes de salvar.
A prévia requer leitura e compilação; aplicar e desfazer requerem permissão de escrita.
Salvar não ativa o pipeline. Alterações concorrentes impedem aplicar ou desfazer.

As propostas ficam na memória por uma hora. Reiniciar o servidor invalida a prévia e
o desfazer. Referências novas de câmera, fonte, modelo ou outro recurso devem ser
configuradas pelo editor; este primeiro fluxo reutiliza as referências e os tipos de
operadores existentes. Isso limita o contexto enviado ao modelo; adicionar tipos de
etapas diferentes ainda exige o editor.
O modelo não executa Python ou comandos. Falha do provedor ou da validação mantém o
pipeline original. O resumo e os detalhes técnicos precisam de revisão humana.

A criação está limitada às receitas do catálogo, com o modelo de detecção padrão disponível
no servidor local. Seleção livre de operadores/modelos/servidores exige o editor. Falta de
modelo pronto impede a proposta. Os ensaios não certificam qualidade geral do modelo nem
respostas narrativas sobre o histórico. Propostas textuais Ollama usam `think: false` para
evitar geração interna desnecessária; o prazo total continua limitado a 120 segundos.
