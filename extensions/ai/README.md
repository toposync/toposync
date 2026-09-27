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

## Exact history metadata filters

The notification and history search endpoints accept `data_filter` as a JSON query
parameter, for example `{"camera_id":"patio","area_id":"water"}`. The internal
`notifications.list`, `ai.search_notifications` and `ai.search_visual_notifications`
services accept the same dictionary directly. All fields must match: comparisons
are exact, case-sensitive strings within `payload.data`, before pagination and the
semantic corpus limit. Nested fields use simple dotted paths. Missing, numeric,
array or object values do not match text. At most eight fields, sixteen path
segments, 256 characters per path and 512 per value are accepted; HTTP input is
limited to 8192 characters. Invalid filters return 422.

The same filter applies to date-only queries, consistency reads and literal
fallback. Filtering an identity field does not establish or strengthen identity;
callers must distinguish confirmed evidence from suggestions. Existing global
priority counts remain global. The history filter offers camera and area selectors under **Camera and area**,
with names from persisted evidence and exact identifiers in requests. Selection
persists locally; missing historical names and deleted selections remain explicit.
`GET /api/notifications/data-options?field=camera_id&label_field=camera_name`
returns distinct text values from all visible history, using the latest record's
label for each value. It uses the existing notification read permission and
cancellable request worker. It does not enumerate silent records or imply that a
historical camera is currently available. The same generic endpoint supports area
metadata. Live updates that leave the selected location are removed from results;
new semantic matches still require refreshing the search.

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
`search.returned_count` is the number of returned records. `results_truncated`
means more matching literal records or indexed semantic candidates existed than
fit in the requested result limit. For semantic search, `corpus_truncated`
separately reports older notifications outside the 250-record corpus; `pending`
counts only unindexed records inside that corpus. These flags describe the reads
used by that response, not an atomic snapshot or physical recording coverage.
A summary must retain these limits and must never equate a full index with a
complete record of activity.

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


## Respostas extrativas com fontes

`POST /api/ai/history/answer` recebe `question` e `profile_id` opcional. Usa a
busca existente, preserva seus limites e pede ao perfil Ollama local que selecione
campos completos de título/descrição. Referência inexistente, texto modificado,
recorte parcial de frase e campos adicionais são rejeitados. O servidor relê as
fontes após a inferência; alterações ou exclusões invalidam a resposta. Conteúdo
dos registros não chama serviços de configuração. `GET /api/ai/history/profiles`
reutiliza descoberta dos modelos instalados sob `core:notifications:read`, sem
exigir permissão de consultar pipelines.

A API também aceita `data_filter` com os mesmos filtros exatos do histórico,
`mode=text|visual`, `identity_id` explícito e `association=human|recognized`.
A seleção humana é padrão; sugestões nunca entram. A visão fornece a seleção
privada autorizada pela mesma função usada na recuperação de notificações.
Sem essa extensão, uma consulta com identidade falha com 503, sem ampliar a
busca. Uma identidade sem eventos associados não chama embeddings nem geração.

Antes e depois da geração, o servidor verifica acesso e associações atuais.
Mudanças de vínculo retornam 409; acesso revogado retorna 403. Fontes que saíram
dos filtros, foram apagadas ou se tornaram silenciosas não sustentam citações.
Falha do modelo conserva somente fontes atuais dentro da seleção. Respostas
usam `Cache-Control: private, no-store`. Biometria, galeria e payload bruto nunca
entram no modelo. A recuperação visual pode ordenar imagens, mas a resposta
continua extrativa de títulos/descrições, sem nova interpretação visual.
Na interface, “Restringir busca e escolher modo” abre seletores de pessoa/pet,
câmera/área e recuperação textual/visual. Opções vêm da galeria autorizada e dos
metadados do histórico; falha de carregamento não apaga seleção ativa. Trocar
perfil de identidade restaura confirmação humana. Mudanças de pergunta/filtro,
curadoria e retorno de foco invalidam resultados; respostas antigas são
ignoradas após cancelamento do consumo HTTP. Isso não certifica interrupção do
processamento no servidor. Erros de acesso, associação alterada e seleção
indisponível são distintos. A ação principal continua “Consultar acontecimentos”.

A interface apresenta os campos como citações, liga cada um ao registro original
e conserva horário de criação e imagem disponível. Durações só aparecem quando
já descritas na fonte; não são inferidas entre notificações. Campos maiores que
2500 caracteres ficam fora da seleção do modelo, mas o registro original continua
consultável. São até dez registros recuperados, sem certificação de cobertura,
identidade, relevância ou veracidade do conteúdo original. Não há análise visual
na resposta textual. Ausência, perfil indisponível ou seleção inválida mantêm um
estado explícito e acesso às fontes. Uma pergunta sobre a tarde sem hoje/ontem
pede definição de dia. A compreensão temporal permanece no vocabulário finito.

Essa síntese extrativa não equivale a uma narrativa livre validada sobre a rotina:
precisão de seleção, casos completos de gatos/visitantes e cobertura representativa
precisam de avaliação independente com o modelo real.


### Limite de memória da inferência textual

O assistente textual exige Ollama0.34.4 ou posterior, com suporte a `truncate`
e `shift` no [contrato da versão](https://github.com/ollama/ollama/blob/v0.34.4/api/types.go).
Solicita contexto32768, saída máxima8192tokens, `truncate=false`, `shift=false`
e retenção de30segundos. Entradas que não couberem falham; não são cortadas para
produzir uma resposta plausível. Apenas `done=true` com `done_reason=stop` é aceito,
inclusive quando uma saída cortada ainda forma JSON válido. O limite prévio de
64000caracteres do roteador continua sendo um teto de entrada, não uma promessa de
que qualquer texto desse tamanho cabe no modelo. Modelo/perfil indisponível ou
requisição excessiva preserva a alternativa manual. Chamadas visuais mantêm seu
contrato próprio. Esses limites reduzem reserva de contexto, mas não limitam o
tamanho dos pesos de um modelo escolhido pelo usuário.

### Anotação visual sem filtrar a ocorrência

`ai.condition_filter` aceita `emit_mode: annotate` quando uma condição visual deve
complementar um evento existente sem apagar resultados negativos. O lifecycle,
identificador, artefatos e demais dados do evento são preservados. O resultado fica
em `payload.ai.condition_filter`: `matched`, `not_matched`, `unknown` ou `closed`.
Consuma `status` junto de `matches` e `confidence`; uma resposta positiva abaixo do
limiar continua `unknown`.

Esse modo exige evidência de captura com publicação recente (limite configurado por
`evidence_timeout_seconds`). Não reaproveita decisões de outro quadro. Repetição,
intervalo entre avaliações, imagem ausente, falha de inferência, falta de capacidade
ou saturação conservam o evento com estado desconhecido. CLOSE não faz inferência.
O limite de streams existente também limita esse estado; entradas sem atividade
expiram para liberar capacidade. A chamada é limitada a 30 segundos. Isso certifica
somente o contrato de software: resultado do modelo requer validação visual própria,
e publicação recente não certifica sincronização ou ausência de buffering na câmera.

`annotation_labels` associa textos definidos pela receita aos quatro estados. Eles
são expostos como `payload.ai.condition_filter.description`, sem copiar conclusões
livres do modelo para a notificação. Não use o rótulo positivo quando `status` for
`unknown`. A receita visitante utiliza recorte estrito do sujeito e preserva o
visitante quando esse recorte não existe; imagem de outra detecção não o substitui.

Quando a anotação positiva usa um artefato já armazenado, `observation` registra a
referência da imagem, publicação, confiança e modelo. O texto identifica uma imagem
passada desta ocorrência; `current_state_verified=false` impede interpretá-la como
estado atual. Grave o recorte antes da inferência com `drop_data_after_store=false`
para conservar os pixels necessários à avaliação. Sem referência armazenada, não é
emitida essa evidência histórica.

`core.notify.historical_evidence_paths` pode reter o último valor não nulo de cada
caminho selecionado, em `data.historical_evidence`, separadamente dos dados atuais.
A receita de visitante seleciona `ai.condition_filter.observation`. A retenção usa
os limites da projeção existente: até 16 caminhos e 128 KiB, sem pixels; dados
inválidos não substituem a evidência anterior. Atualizações limitadas por intervalo
ainda alimentam o histórico. CLOSE normal ou sintetizado no desligamento conserva
a evidência; outra ocorrência começa sem herdá-la. É a última evidência por caminho,
não um arquivo completo de todas as avaliações ou de todas as imagens.

### Imagens e orçamento de inferência Ollama

Arrays de frames seguem a convenção BGR/BGRA do pipeline; a codificação converte
para RGB/RGBA. Imagens PIL e arquivos já codificados mantêm suas cores originais.
Ollama usa contexto32768, saída limitada a8192tokens e retenção30s tanto para texto
como para visão. Respostas incompletas e truncamento de contexto são recusados;
essa garantia exige Ollama0.34.4 ou posterior. Os limites não constituem garantia
de latência ou precisão; valide o modelo e as cenas do ambiente de implantação.

### Visitante e campainha no assistente

A receita exige câmera, área mapeada e associação explícita ao nome de um sinal
booleano descoberto nas configurações da câmera. O assistente lê somente a
 descoberta recente mantida em memória pelo gerenciador ONVIF; não inicia
 descoberta, captura ou assinatura de eventos. A consulta do catálogo exige
 também leitura de configurações, como a consulta de sinais da câmera.

Sinal ausente ou expirado exige descoberta explícita na câmera. Um único sinal
não implica campainha, e movimento não comprova acionamento. A prévia apresenta
o nome escolhido; salvar revalida a descoberta sem contato com o dispositivo e
mantém o fluxo desativado. A correlação temporal não identifica quem acionou e o
indício visual de pacote não identifica profissão ou entrega. Cache desaparece
no reinício e expira conforme o prazo do gerenciador. Suporte físico e precisão
exigem evidência independente; testes controlados não os certificam.
