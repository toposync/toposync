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

## Assistente de alteração de pipelines

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

Este fluxo ainda não certifica criação por linguagem natural, descoberta completa de
recursos, qualidade geral do modelo nem respostas narrativas sobre o histórico.
