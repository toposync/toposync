# Sensores visuais: configuração, uso e recuperação

O operador **Sensor visual** (`vision.visual_state`) transforma uma região observável
da câmera em classes configuradas por você: porta aberta/fechada, piscina
coberta/descoberta ou roupa presente/ausente no varal. O recurso é experimental.
Uma classe descreve aparência visível; não prova funcionamento elétrico, ausência
de qualquer obstáculo ou segurança para movimentar um portão.

Configure primeiro um fluxo desativado e um destino de observação sem automações
de atuação. Antes de usar o resultado como informação complementar em alarmes,
execute o [protocolo de instalação](validacao-de-instalacao.md) com autorização
para aquela instalação. Os mecanismos de software não substituem proteções físicas.

## Compatibilidade desta implementação

Esta documentação descreve a linha de desenvolvimento que contém o commit
`5429500e3c74294f55622a74530554bbfd95b940`. Não é anúncio de versão publicada.
Instale núcleo, extensões e interfaces do mesmo conjunto de código e registre
a revisão exata: os números abaixo também podem existir em builds anteriores
sem todas as capacidades.

| Componente | Identificação declarada | Condição adicional |
| --- | --- | --- |
| Núcleo Toposync | `0.9.0.dev0` | Caminho de captura de referências e revisão de edição de pipeline disponíveis |
| Vision | `0.9.0.dev0`, requer núcleo `>=0.9.0.dev0` | Interface e servidor da mesma revisão; execução local |
| Cameras | `0.1.17` | Origem, preparo e identidade de captura compatíveis |
| AI, para contingência ou modo direto | `0.1.14` | Perfil multimodal compatível, permissão de imagens e orçamento |
| Integração Toposync no Home Assistant | `0.1.0` desta revisão | Coordenador de estados visuais, plataformas nativas e expiração no receptor |
| Home Assistant usado nos testes isolados | `2026.10.2`, Linux ARM64 | Outros ambientes precisam de validação própria; ver o [README da integração](../../integrations/home_assistant/README.md) |

Não selecionar um servidor remoto para este operador. Um host antigo sem captura
de referências apresenta uma mensagem de incompatibilidade. A ausência da rota de
registros também é informada; se faltar somente a avaliação operacional, ela fica
explicitamente não avaliada. A integração trata ausência do endpoint de estados
como indisponibilidade, nunca como a classe negativa.

Para atualizar, preserve uma cópia privada do diretório de dados e exporte o sensor.
O banco de referências usa schema **5**, com migração das versões 1–4. Código antigo
recusa esse schema. Para voltar de versão, use dados de backup compatíveis ou uma
instalação limpa separada; não altere manualmente o número de versão do banco.
Um ZIP do sensor não substitui o backup completo da instalação.

## Criar o primeiro sensor

1. Na configuração da câmera, escolha a receita **Sensor visual** e a fonte de
   vídeo. O fluxo gerado começa desativado. Também é possível adicionar
   `vision.visual_state` a um pipeline manual com uma origem de câmera anterior.
2. Abra o operador no editor do fluxo. Escolha um recorte anterior fixo ou uma
   região canônica na panorâmica. Use **Permitir imagem inteira quando não houver
   recorte anterior** somente se o quadro inteiro for a região desejada.
3. Crie duas classes iniciais. Em **Nome**, escreva, por exemplo, `Aberta` e
   `Fechada`. A chave técnica deve começar com letra minúscula e usar somente
   letras minúsculas, números e sublinhado, até 64 caracteres: `aberta`, `fechada`.
   `unknown` e `unavailable` são estados reservados, não classes.
4. Em **O que identifica este estado**, descreva a diferença observável:
   “o vão à direita da folha está visível”. Evite descrições como “seguro para
   fechar”, pois a câmera pode não observar toda a zona de risco.
5. Com a cena no estado correto, pressione **Capturar** dentro daquela classe.
   A miniatura aparece ali. Amplie para conferir, remova capturas erradas e use
   **Desfazer remoção** quando necessário. A captura usa o preparo configurado
   e pode ocorrer independentemente do filtro de movimento; continua exigindo
   imagem recente, identidade de captura e região válidas.
6. Comece com poucas aparências distintas por classe, incluindo dia e noite quando
   disponíveis. Várias fotos quase idênticas não substituem uma aparência noturna.
   A configuração aceita 2–32 classes, ao menos duas habilitadas, até 30 referências
   por classe e 300 referências no total. O limite não é uma recomendação de volume.
7. Escolha a estratégia abaixo, aplique seus parâmetros e use **Testar agora**.
   O teste é isolado do destino de publicação, mas pode capturar imagens e consumir
   o orçamento do perfil de IA. Use somente fontes e provedores autorizados.
8. Confira classe, candidato, motivo, idade e revisão do resultado. Salve o pipeline
   ainda desativado, reabra-o e confira as miniaturas. Habilitação e publicação são
   passos posteriores à revisão; salvar um rascunho não prova qualidade de visão.

O identificador interno da classe é diferente do nome e da chave. Mapeamentos
binários usam esse identificador; renomear não deve exigir recriar a entidade.
Para um sensor independente, duplique o pipeline: a cópia recebe nova identidade
e começa com publicação desativada. Restaurar um backup preserva a identidade.

## Escolher a estratégia

| Opção do painel | Recursos necessários | Comportamento |
| --- | --- | --- |
| Comparar exemplos localmente | Modelo de imagem instalado, referências em todas as classes habilitadas e limiares calibrados | Não faz chamadas à IA multimodal |
| Usar IA em caso de dúvida | Tudo do modo local e um perfil multimodal | Consulta IA para uma imagem válida mas ambígua; não recupera uma região invisível |
| Analisar sempre com IA | Perfil multimodal e descrição ou referência em cada classe | Cada análise depende do provedor; resposta rápida precisa de medição própria |

Em **Análise local**, use **Consultar modelos disponíveis** e escolha um modelo
compatível instalado. O produto não escolhe um modelo nem limiares universais
silenciosamente. Na ausência de um pacote validado para a instalação, essa etapa
exige preparação pela engenharia antes de habilitar a comparação local.

O pacote deve declarar origem, licença, SHA-256, normalização, entrada ONNX de
imagem `float32` NCHW com tamanho fixo e saída vetorial de dimensão explícita.
O contrato é `task=embedding`, runtime `onnxruntime` e pós-processamento
`image_embedding`; listar o manifesto não comprova que o artefato está instalado,
que o acelerador funciona ou que as classes serão separáveis. A execução verifica
arquivo, hash e formatos. Consulte a [implementação do contrato](../../extensions/vision/src/toposync_ext_vision/processing/runtime_backends/embedding.py).

Para um pacote preparado pela engenharia, o servidor descobre manifestos em
`vision-manifests` dentro de seu diretório de dados. Também aceita um diretório
explícito em `TOPOSYNC_VISION_MANIFESTS_DIR` ou caminhos separados por vírgula em
`TOPOSYNC_VISION_MANIFEST_PATHS`. Use o diretório da instalação autorizada e mantenha
os pesos no caminho declarado pelo manifesto. Confira o catálogo e execute uma
inferência de validação no ambiente isolado antes de selecionar o modelo no sensor.
Não renomeie um detector de objetos como embedding: o contrato de saída é diferente.

Calibre **Limiares de comparação** com sessões separadas das referências: similaridade
mínima e vantagem sobre a outra classe para entrar e manter um estado. Similaridade
não é probabilidade. Manutenção não pode ser mais exigente que entrada. Trocar o
modelo preserva fotos e limpa esses parâmetros para recalibração. Não ajuste o
limiar olhando o conjunto reservado de avaliação final.

Para IA, descreva também o **Contexto da cena para a IA**, selecione o perfil e
confira quais referências serão enviadas. Imagens externas exigem permissão no
sensor e compatibilidade do provedor; credenciais ficam nos ajustes de IA. Limites
do sensor, do perfil e da conta continuam aplicáveis ao teste. O prazo efetivo
é limitado pela idade útil restante da imagem, mesmo que o prazo nominal seja
maior. Resposta tardia não muda o estado nem renova sua validade.

## Cadência, estabilidade e desconhecimento

A receita mantém uma origem contínua, permite passagem ociosa no filtro de
movimento e usa o limitador existente. Em um fluxo manual, não dependa somente
de um evento ONVIF: uma mudança lenta ou um evento perdido pode ficar invisível
ao pipeline. Resolva os alertas de cadência; o editor não modifica o grafo por você.

| Parâmetro | Padrão atual | Significado |
| --- | --- | --- |
| Intervalo de análise | 250 milissegundos | Espaçamento de admissão; não é garantia de conclusão nesse prazo |
| Confirmações | 2 | Capturas distintas e concordantes |
| Separação mínima | 150 milissegundos | Reenvio da mesma captura não é um voto novo |
| Intervalo máximo entre confirmações | 750 milissegundos | Evidência muito espaçada reinicia a sequência |
| Retenção do estado anterior durante dúvida | 500 milissegundos | Nunca estende a validade original indefinidamente |
| Idade máxima do quadro | 750 milissegundos | Resultado sobre imagem vencida não confirma |
| Validade sem nova evidência | 2.000 milissegundos | O valor deixa de ser atual mesmo se a inferência ou rede estiver bloqueada |

Esses padrões não são resultados de desempenho. Permanência mínima por classe,
quando configurada, exige observações cobrindo o período; esperar sem imagens não
confirma. Meça a menor duração de evento observável e os atrasos do equipamento.
Não aumente validade para esconder um provedor que não acompanha a cena.

A receita limita a passagem sem movimento a uma imagem a cada 500 milissegundos,
mesmo com intervalo de análise de 250 milissegundos no sensor. Uma mudança que
começa e termina entre essas imagens pode não ser observada; uma única imagem
da nova classe também não satisfaz as duas confirmações. Reduzir somente o
intervalo do sensor não aumenta a cadência entregue pelos operadores anteriores.

**Desconhecido** significa que não há classe confirmada atual. **Indisponível**
significa que a observação ou execução não está disponível. Nenhum dos dois equivale
a “fechada”, “desligada”, “vazio” ou “livre”. Candidato ainda não é confirmação.
Último estado conhecido pertence ao histórico, com horário próprio.

Na panorâmica, o recorte depende de geometria e contexto de alinhamento válidos.
O alvo móvel deve ser excluído do contexto usado para alinhar. Região fora do campo,
cobertura parcial ou localização recusada não produz classificação negativa.
Ao retornar, novas observações precisam confirmar. Ter geometria válida não prova
que o alvo está visível: oclusão, reflexo e saturação precisam de exemplos e ensaios.

## Revisar sugestões e condições noturnas

Sugestões de IA ficam separadas das referências utilizadas na decisão. Confira a
imagem ampliada, a origem e a classe proposta; aceite, corrija ou rejeite. Aprovar
altera o rascunho: salve o pipeline para aplicar. Desfazer permite revisar um erro
dentro das condições mostradas no painel. A confiança declarada pela IA não é
verdade de referência nem autorização para promoção automática.

Uma sugestão noturna revisada pode reduzir consultas futuras, mas isso precisa ser
medido em outras sessões. Não reutilize a mesma imagem como prova independente.
Há limites de quantidade e retenção para sugestões; imagens aprovadas e rascunhos
têm regras próprias de armazenamento. Remover uma miniatura não é garantia de
eliminação imediata de todas as cópias, backups ou referências compartilhadas.

## Publicar no Home Assistant

Use a [integração nativa](../../integrations/home_assistant/README.md), com token
de serviço e permissão de leitura `visual_state:<sensor_id>`. O endereço do
servidor precisa ser alcançável pelo Home Assistant; o endereço do navegador
pode ser diferente. Não use um cookie de sessão temporário como token de serviço.

No operador, abra **Publicação no Home Assistant → Prévia e mapeamento**. Escolha
**Estados nomeados** para classes livres ou **Binário (ligado/desligado)**. No binário,
mapeie explicitamente quais classes representam cada lado, sem sobreposição.
Classes não mapeadas continuam desconhecidas. Confira a prévia, habilite publicação,
aplique as configurações e salve o pipeline. A prévia não chama serviços físicos.

O receptor desconta o tempo da requisição da validade remanescente e expira o
estado por temporizador próprio. Reinício não restaura um valor histórico como
atual. Perda de rede, permissão ou publicação torna a entidade indisponível;
expiração acessível fica desconhecida. Não substitua esse caminho pelo publicador
booleano legado ou por `input_boolean`, que não preservam o mesmo contrato.

## Investigar e recuperar

| Situação | Próximo passo |
| --- | --- |
| Sem modelo, hash diferente ou revisão alterada | Reinstalar o pacote correto ou selecionar a nova revisão e recalibrar; preservar as fotos |
| Sem região observável | Conferir origem, enquadramento, recorte e geometria; não forçar uma classe |
| Desconhecimento frequente à noite | Revisar recorte e exemplos noturnos independentes; medir cobertura antes de relaxar limiares |
| IA lenta ou cota atingida | Conferir prazo restante, filas e orçamento; considerar comparação local e medir o perfil escolhido |
| Editor acusa conflito ao salvar | Preservar o texto do rascunho antes de recarregar; revisar a versão salva e reconciliar as alterações |
| Requisição cancelada ou expirada ao salvar | Ler o estado salvo antes de repetir; cancelamento não prova que o servidor não gravou |
| Home Assistant indisponível | Conferir conectividade, token, grants, publicação e revisão; não converter o estado para falso |

Em **Backup e restauração**, baixe um ZIP privado antes de mudanças relevantes.
Ele inclui configuração do operador, fotos e descrições; exclui pesos, credenciais,
configuração anterior do pipeline, sugestões pendentes, valor atual e registros
de qualificação. O máximo é 64 MiB. Importar apresenta uma revisão antes de aplicar;
aplicar muda o editor e salvar persiste. A publicação restaurada fica desativada.

Para reversão, desative e salve o fluxo, confira a indisponibilidade no receptor,
restaure o sensor no lugar, confira também origem e preparo anteriores e salve
desativado. Após revisão, iniciar a execução exige capturas posteriores à sua
inicialização; quadros antigos no buffer não podem confirmar. Detalhes de retenção,
idempotência e API estão no [README Vision](../../extensions/vision/README.md).

Em **Registros de validação**, **Consultar registros salvos** lê relatórios da revisão
salva, com data da consulta. Edição oculta o resultado anterior; salve e consulte
novamente. Esse painel não é monitoramento contínuo. Um relatório favorável pode
coexistir com suspensão operacional por silêncio, cadência ou execução indisponível.
Ausência de impedimentos observados não certifica precisão, latência ou segurança.
Registro de relatório revisado é uma operação explícita da API documentada;
**Testar agora** não cria aprovação. Mudanças de instalação ou recursos exigem nova
revisão; atualmente qualquer gravação da configuração suspende conservadoramente
a compatibilidade anterior do relatório.

## Limites a manter visíveis

O recurso não certifica passagem livre e não comanda equipamentos por si. Vídeo
sem timestamp físico verificado só permite declarar idade desde a recepção, não
atraso da câmera. Infraestrutura temporal testada não comprova acurácia com chuva,
infravermelho, oclusão ou uma câmera diferente. Mantenha cada instalação em
observação até obter evidência independente e orçamento de erro adequado ao uso.
