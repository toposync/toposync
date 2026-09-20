# Pose, pés e gestos

Recurso experimental, composto por operadores de visão e câmera. Estimativas não são medições físicas nem autorização para acionar dispositivos. Precisão em oclusões reais, gestos naturais, alvos e condições de vigilância precisa de qualificação independente na cena de uso.

## Configuração inicial

No editor de fluxos, escolha o servidor de processamento antes de preparar os modelos. Adicione as etapas nesta ordem:

```text
Usar câmera -> Detectar objetos (anotar todos os quadros) -> Estimar pose
 -> Acompanhar objetos -> Estimar pessoa e pés no mapa
 -> Reconhecer gestos ao longo do tempo -> Estimar alvo apontado no mapa
```

O detector fornece as pessoas; a pose não instala outro detector ou rastreador. Use `MediaPipe Pose 33` em Estimar pose e limite o número de pessoas conforme o orçamento da máquina. O artefato é baixado antes da execução, verificado pelo hash do manifesto e instalado no servidor escolhido. Ausente, incompatível ou com download falho não deve aparecer como pronto.

O manifesto fixa o modelo OpenCV Zoo na revisão `47534e27c9851bb1128ccc0102f1145e27f23f98`, com SHA256 `9d89c599319a18fb7d2e28451a883476164543182bafca5f09eb2cf767ed2f3f`. A configuração de entrada e o decodificador pertencem ao manifesto/backend, não ao fluxo do usuário. Os pesos não são incorporados ao pacote do Toposync. A procedência e as condições de código/pesos constam em `extensions/vision/manifests/mediapipe_pose_33.json`; isso não concede direitos sobre o modelo corporal GHUM nem sobre os dados de treinamento.

Prefira anotação contínua para manter gestos sustentados e detectar seu encerramento. Um filtro de movimento anterior pode suprimir as amostras necessárias. Depois que o rastreador separa os pacotes por pessoa, configure as conexões como **Mais recente por sujeito**, com capacidade suficiente para os sujeitos ativos; uma única fila que mantém apenas o último pacote pode apagar atualizações de outra pessoa. Nenhuma dessas etapas precisa de Salvar imagens.

### Alternativa experimental RTMPose-M Halpe26

O catálogo também oferece `RTMPose-M Halpe26 (experimental)` para pose bidimensional de corpo e pés. Selecione o servidor de processamento, abra o guia do modelo e baixe o arquivo ZIP da revisão fixada. Extraia `end2end.onnx` e envie esse arquivo pelo upload do modelo. Não envie o ZIP nem um checkpoint PyTorch. O manifesto fixa o ONNX de 55.685.444 bytes, SHA256 `26f3a19e61304a600dfb82d1001d41d24343b89fc70a33ffc84657e0b0bf2ecf`; o upload verifica hash e contrato antes de substituir um artefato existente. Não há download durante o primeiro frame nem redistribuição dos pesos pelo Toposync. Código, declaração de licença dos pesos e limites sobre dados de treinamento estão separados no manifesto.

Essa opção reutiliza as detecções de pessoas, sem outro detector ou rastreador. O esqueleto `halpe26` mantém nomes próprios, incluindo `big_toe`, `small_toe` e `heel`. Para a hipótese de apoio no chão, usa calcanhar e dedo grande, com `anchor_type=heel_big_toe_support_midpoint` e os nomes de origem em `support_landmark_names`; não afirma que o dedo grande é o ponto `foot_index` do MediaPipe. Contato físico permanece não verificado.

Os scores dos landmarks são respostas SimCC brutas, não probabilidades e não são comparáveis aos scores MediaPipe. Podem exceder 1 e são preservados. Gestos aceitam esse domínio somente quando esqueleto e `metadata.landmark_score_semantics=simcc_min_axis_maximum` concordam; o limiar mínimo configurado permanece inalterado e requer qualificação para cada modelo. O `score` legado da pose conserva a confiança da detecção de origem, indicada por `metadata.pose_score_source=source_detection`; não é uma confiança global da pose.

RTMPose-M Halpe26 não fornece profundidade nem landmarks 3D relativos. Portanto, não habilita apontamento espacial e não comprova consistência tridimensional de apoio para prever pés ocultos. MediaPipe permanece disponível para o caminho experimental que exige sua saída 3D relativa. Pré-processamento e decoder RTMPose seguem a transformação afim Python MMPose com padding 1,25 e entrada RGB 192×256, não o recorte arredondado do SDK MMDeploy. CPU é o único provider avaliado. Não há qualificação de precisão anatômica, gestos, oclusões ou paridade com o checkpoint PyTorch.

## Calibração e grandezas distintas

Vincule a câmera a um elemento da composição. Nas propriedades do elemento, abra **Calibrar chão e geometria métrica**. O mapeamento do chão e a geometria métrica têm estados separados. Uma transformação plana pronta não implica extrínsecos de câmera nem altura de ombro conhecida.

Informe parâmetros medidos da lente da fonte e resolução correspondentes. Os valores `fx/cx` são divididos por largura menos um; `fy/cy`, por altura menos um. Coeficientes de distorção são adimensionais e devem ser preenchidos, inclusive zeros medidos. Identidade permite o mapeamento plano legado, mas não fornece intrínsecos métricos. Dimensões sugeridas pela configuração da fonte não comprovam a resolução de uma calibração medida.

A imagem precisa ter sido decodificada pelo navegador e pertencer à fonte e vista atuais. Resoluções divergentes bloqueiam marcação e ativação; o editor mostra as dimensões observadas sem redimensionar intrínsecos automaticamente. Corrija a fonte ou informe a calibração correspondente. Alterar parâmetros invalida a solução anterior.

Use ao menos seis pontos de ajuste no chão e dois pontos independentes de conferência. Pontos da imagem e do mapa precisam representar o mesmo lugar físico; não marque correspondências arbitrárias para obter estado pronto. Não extrapole além do domínio validado. Câmeras móveis exigem evidência de vista compatível no instante da imagem.

As saídas espaciais distinguem:

- Projeção corporal no chão: hipótese geométrica obtida do tronco e de um intervalo amplo de estatura, não a posição exata de um pé.
- Pé esquerdo e direito: hipóteses individuais de apoio. A combinação de calcanhar e ponta do pé não comprova contato físico.
- Pé oculto: previsão curta a partir de apoio anterior compatível com pouca movimentação; a incerteza cresce e o resultado expira. A previsão não alimenta a si mesma como nova observação.
- Alvo apontado: seleção espacial separada da localização corporal. Não substitui a âncora da pessoa.

O prior de estatura não identifica idade, gênero ou outro atributo demográfico. Não estreite o intervalo a partir de previsões anteriores. Sentado, deitado, pé elevado e posturas incompatíveis exigem abstenção. Ambiguidade monocular pode persistir mesmo com boa reprojeção: uma hipótese não vira medição por parecer plausível.

## Contratos para consumidores

| Campo | Semântica |
|---|---|
| `vision.poses` | Poses de imagem, esqueleto explícito, landmarks identificados, unidades e referencial. |
| `vision.pose_frame_packet_id`, `vision.pose_media_ts` | Quadro e instante que originaram a inferência; o rastreador pode criar outro envelope sem refazer a pose. |
| `subject.id` | Identidade do evento individual do rastreador; não é reconhecimento biométrico. |
| `spatial.person_ground` | Corpo, hipóteses de cada pé, procedência, incerteza, prazo e motivos de indisponibilidade. |
| `vision.gestures` | Estado temporal, ator e evidência atual. O modo de eventos cria ciclo próprio do gesto, não confunde sua duração com a presença da pessoa. |
| `spatial.pointing` | Origem/direção tridimensional alinhadas ao mundo, candidatos, ambiguidade, alvo e diagnóstico. `actions_authorized` permanece falso. |
| `capture_evidence` | Proveniência da captura e publicação. Idade por publicação do decoder não equivale a idade física da exposição. |

Landmarks ausentes permanecem nulos no índice original. Coordenadas fora da imagem não são presas à borda no contrato novo. O score da rede não é probabilidade calibrada nem prova de visibilidade. Representações legadas continuam disponíveis; não as use para inferir semântica mais forte do que fornecem.

Gestos exigem tronco utilizável, mas avaliam cada braço separadamente. `vision.gestures.side_evidence` distingue evidência `available` de `unknown` por lado; disponível não significa braço levantado nem visibilidade física comprovada. Um cotovelo ou punho incerto não invalida o outro braço. `hand_raised` afirma somente que o lado indicado atende à regra, sem afirmar que a outra mão está abaixada. `both_hands_raised` exige evidência positiva dos dois lados.

O estado global é `active` somente com episódio confirmado e evidência positiva atual; `unknown` quando falta evidência e não há episódio atualmente sustentado; `none` quando a avaliação é completa e nenhum episódio está sustentado. Candidatos ainda em confirmação podem coexistir com `unknown` quando só um braço é utilizável. A perda de evidência interrompe a confirmação e o histórico de aceno daquele lado. Um episódio pode aguardar o prazo de liberação, mas recebe `evidence_current=false` imediatamente; ao expirar com evidência ausente, fecha com motivo `evidence_unavailable`, sem concluir que a mão baixou. Retorno após a lacuna não herda o limiar de saída de um episódio bilateral retido.

Um episódio cujo prazo de liberação venceu não pode ser reaberto nem emprestar seu limiar de manutenção à confirmação seguinte. Mesmo com cooldown zero, o próximo episódio exige novamente o limiar de entrada e a duração mínima.

Geometria métrica usa metros e eixos `x_y_up_z`. A saída tridimensional relativa ao quadril do modelo não é, sozinha, uma coordenada do mapa. Apontamento requer alinhamento geométrico, escala, translação, reprojeção compatível, apoio condicional atual e calibração correspondente. Paredes usadas como oclusores/alvos precisam de altura física explícita; a altura visual do tema não é uma medida.

`CLOSE`, idade excessiva, mudança de vista/calibração, troca de geração de captura ou identidade invalidam derivados. Consumidores devem expirar o que mostram sem renovar o prazo ao renderizar, receber novamente ou abrir uma notificação antiga. Não sobreponha uma pose à última miniatura disponível sem comprovar que imagem, captura e transformações correspondem.

Na execução por servidor de processamento, perder a conexão encerra as observações
da origem afetada e descarta suas filas. O fechamento sintetizado pela origem usa
`shutdown_synthesized`; não é uma confirmação enviada pelo servidor desconectado.
Após reconectar, novas amostras iniciam novas observações, sem acrescentar a
indisponibilidade à duração anterior. Uma reconexão transitória pode conservar o
mesmo sujeito remoto, mas não reabre o registro de notificação já fechado. Reiniciar
o rastreador gera novos identificadores de evento e stream; `event_code` é somente
o número sequencial de exibição, não uma identidade persistente.

Servidores atualizados identificam a época dos eventos em `event_epoch`. O cursor
de retomada e o reconhecimento de recebimento usam essa época para não confundir
contadores reiniciados; mensagens de outra época ou duplicadas não são admitidas.
Depois de negociar uma época, mensagens sem essa informação também são ignoradas.
Compatibilidade legada sem época não oferece essa proteção. O reconhecimento
confirma admissão na fila, não execução do consumidor. Falha isolada desse
reconhecimento não encerra o canal de dados nem garante entrega exatamente uma vez.

As pontes distribuídas preservam as políticas do grafo atual. Antes de separar
destinos, a fila intermediária não compacta atualizações de sujeitos: um pacote
para outro destino não pode substituir o pacote aguardado. A política configurada
volta a valer após esse roteamento. Isso não garante entrega de todas as atualizações
sob saturação; os limites de fila e os timeouts continuam observáveis.

Os operadores espaciais e de gestos mantêm um registro terminal limitado a 4096 identidades encerradas
por instância. Não descartam silenciosamente esse registro para reutilizar sujeitos: ao
exceder a capacidade, retornam `closed_subject_capacity_reached` até reinicializar a
instância. Isso limita a disponibilidade operacional desta implementação experimental;
não constitui validação de execução indefinida. Um novo evento deve receber nova identidade.

## Campos opcionais em notificações

`core.notify` mantém o payload legado por padrão. No painel avançado, **Campos adicionais do payload** permite incluir caminhos específicos, por exemplo `vision.poses` e `spatial.person_ground`. Cada caminho usa identificadores separados por pontos, sem curingas; são permitidos até 16 caminhos de 256 caracteres.

Os dados selecionados ficam em `notification.payload.data`. `data_projection` informa disponibilidade por caminho. Limites de profundidade, quantidade e tamanho são aplicados à projeção: profundidade 16, 8192 nós JSON e 128 KiB incluindo diagnóstico. Um valor inválido ou excessivo é omitido integralmente, não truncado em uma coordenada aparentemente válida. Mudanças dos campos selecionados participam da detecção de atualizações, respeitando o intervalo da notificação.

Corpo e pés no mapa não dependem de adicionar o operador de apontamento ou de gestos. `camera.person_ground_estimate` inclui `map_revision` em `spatial.person_ground`, vinculado à mesma composição persistida que forneceu a calibração. A interface confere essa revisão e os elementos exibidos antes de desenhar; chão e apontamento de revisões divergentes são recusados. Calibração inline sem vínculo comprovado ao mapa não autoriza sobreposição espacial. Observações anteriores que carregam a revisão somente no apontamento continuam compatíveis. O campo adicional permanece dentro do caminho `spatial.person_ground`, sem ampliar a seleção de 16 caminhos nem persistir imagens.

Esse transporte é opt-in e persiste metadados na notificação; não grava pixels nem cria referência de imagem. O renderer precisa verificar identidade, instante e validade dos campos antes de desenhar. A aparência de uma notificação não demonstra a precisão do estimador.

A duração de uma notificação de pipeline é o intervalo recebido **até a última atualização publicada**, não um relógio que continua contando sem novos frames. `event.started_ts`, `ts` e `duration_seconds` mantêm a linha temporal do episódio; `time_basis` distingue mídia de fallback em `packet_created_at`. Valores de mídia não são datas Unix, mesmo quando sua magnitude se parece com uma data. Amostras atrasadas não reduzem a duração; fechamento sem frame, shutdown e recuperação após reinício não acrescentam tempo sem evidência. O runtime considera também amostras suprimidas por deduplicação ou intervalo ao publicar o fechamento. Mudança conhecida de domínio, instância ou geração da captura torna a duração indisponível (`duration_status: clock_changed`); não soma relógios distintos. Sem metadados de época, não é possível distinguir um seek não declarado de uma amostra atrasada. Datas civis `createdAt` e `updatedAt` continuam separadas para histórico e ordenação operacional. Registros antigos sem base temporal explícita não são extrapolados pelo relógio do navegador.

Se um fechamento contém timestamp, mas perde a referência da captura usada pelo
intervalo, a duração conserva a última amostra comprovada. Esse timestamp terminal
não prolonga o intervalo; ausência de referência não é, sozinha, mudança de relógio.
Uma referência explicitamente conflitante continua tornando a duração indisponível.

Para usar o renderer experimental da extensão de câmeras, conecte **Enviar notificação**
ao fim do fluxo, use o tipo `com.toposync.cameras.human_observation` e selecione os
seguintes caminhos no painel avançado:

```text
subject
camera_id
source_stream_id
capture_evidence
vision.poses
vision.pose_media_ts
vision.pose_frame_packet_id
vision.gestures
spatial.person_ground
spatial.pointing
spatial.camera.status
spatial.camera.frame_packet_id
spatial.camera.camera_id
spatial.camera.composition_id
spatial.camera.physical_view_id
spatial.camera.geometry.calibration_digest
```

Mantenha a chave por `subject.id` e a fila por sujeito. Atualizações devem chegar dentro
da validade da evidência; um intervalo de notificação de um segundo excede o limite
de exibição de 750 milissegundos. Diminuir o intervalo não comprova latência do fluxo.
Prioridade silenciosa mantém histórico, mas não aparece na lista normal.
Para inspecionar a prévia, use prioridade baixa, habilite esse filtro na lista e
abra os detalhes da notificação. A lista mostra apenas um resumo; a imagem efêmera
pertence ao detalhe selecionado. No detector, selecione **Pessoas** em **Objetos de
interesse** e mantenha o modo de anotação, evitando notificações humanas para outros
objetos da cena.

Os detalhes separam corpo, cada pé, gesto e candidatos a alvo. A representação 2D
continua limitada à âncora corporal; não equivale à representação 3D de pés e cone.
Para a prévia experimental sincronizada, habilite a opção de imagem efêmera no
painel de notificação, mantendo atualizações em tempo real. O padrão é desabilitado.
Somente o detalhe selecionado recebe a imagem do mesmo pacote; lista, histórico e
banco continuam sem pixels. Fechar o detalhe, desconectar ou expirar a captura
descarta a imagem. Nenhuma miniatura antiga é usada como substituta.

O transporte aceita imagens de até 256 KiB e 2.097.152 pixels, com validade máxima
de 750 milissegundos desde a publicação da amostra na origem. Isso não comprova o
instante de captura física. Nesta versão, somente imagens completas com geometria
identidade são aceitas; recortes e transformações não recebem fallback. A pose só
aparece após conferir identidade, captura, geometria e dimensões decodificadas.
Pontos vazados e linhas tracejadas representam estimativas, não contato ou
visibilidade comprovados. Landmarks externos são omitidos do desenho e contados,
sem alterar suas coordenadas. Há limite de 16 detalhes com imagem por processo;
cada um mantém apenas a atualização mais recente.

A jornada completa e a qualificação visual permanecem experimentais. O caminho
foi testado com JPEG real e testes de componentes; isso não substitui a inspeção
integrada no navegador com o fluxo e o modelo reais.

As sobreposições verificam a revisão persistida do mapa e os elementos exibidos.
Essa verificação vale no máximo 100 milissegundos desde o início da consulta e é
reavaliada após 50 milissegundos, sem prolongar a validade da captura. Consultas
compatíveis não apagam nem reconstroem a geometria. Falha, mudança conhecida ou
prazo esgotado oculta a sobreposição; resposta atrasada não a revalida. A checagem
não é atômica com edições no servidor: existe uma janela limitada entre consultas.
O custo de até 20 consultas por segundo por sobreposição ativa ainda precisa ser
qualificado com múltiplos sujeitos e vistas. Não usar esse mecanismo para autorizar
ações físicas.

## Diagnóstico e recuperação

- Sem pose: verifique detecções de pessoas, modelo pronto no servidor escolhido, artefato de entrada e limite de pessoas.
- Sem identidade: coloque Acompanhar objetos antes dos operadores temporais por pessoa.
- Sem localização: examine o motivo em `spatial.person_ground`; confira fonte física, lente, pontos independentes, domínio e idade da captura.
- Sem alvo: diferencie câmera métrica ausente, apoio incompatível, pose tridimensional insuficiente, reprojeção rejeitada, nenhum candidato e vários candidatos. Não force o candidato mais próximo.
- Falha ao salvar: o rascunho permanece editável com o erro exposto; corrija a configuração ou o modelo e salve novamente.
- Dados antigos: não aumente a tolerância de idade para ocultar congestionamento. Examine tempo de inferência, idade na origem, filas e descartes separadamente.

Para voltar ao fluxo anterior, desative o fluxo, remova os nós novos e reconecte detecção/rastreamento/mapeamento anteriores. Preserve uma cópia da configuração original. Verifique encerramento dos sujeitos e ausência de sobreposições antigas antes de reativar. Não é necessário excluir calibrações nem apagar modelos para desabilitar a funcionalidade.

No editor de topologia, Tab percorre nós e conexões; Enter seleciona o item e abre seu painel, e Escape limpa a seleção. Excluir um nó também remove suas conexões, sem reconectar os vizinhos automaticamente. Para conectar sem arrastar, selecione o nó de origem e use **Conectar nós** no painel: escolha a porta de saída e o nó/porta de destino, depois **Conectar**. Entradas ocupadas e ciclos são rejeitados sem alterar o grafo. Novas conexões usam as políticas de fila padrão dos operadores; confira essas políticas no painel da conexão antes de salvar. **Desfazer** recupera a edição anterior ainda disponível no histórico local; **Salvar** persiste o fluxo.

## Limites de evidência

Testes sintéticos validam contratos e matemática, não acurácia em pessoas reais. Paridade com a implementação de referência valida o processamento do artefato, não a anatomia. Uma imagem estática repetida pode testar a execução, mas não mede gestos temporais, trocas de identidade, passos ocultos nem falsos eventos por hora. Um ensaio curto de latência não aprova estabilidade prolongada. Qualifique separadamente corpo, cada pé, contato, direção e entidade, com cobertura e abstenção explícitas.
