# Protocolo de validação de uma instalação de sensor visual

Este protocolo prepara um ensaio; sua existência não autoriza captura, movimento
de câmera, uso de imagens privadas, chamadas pagas ou atuação. Execute campo somente
com autorização explícita para as fontes, pessoas, condições e período identificados.
Use o [guia de configuração](guia.md) antes de iniciar. Responsável pelo documento:
Mateus Calza.

O resultado se aplica à combinação observada de instalação, região, classes,
modelo, referências, política temporal, perfil de IA e consumidor. Não certifica
segurança funcional, passagem livre ou capacidade de detectar qualquer obstáculo.
Os resultados devem distinguir **software**, **replay**, **hardware** e **campo**.

## 1. Congelar o contrato antes da avaliação

Crie um diretório privado fora do repositório para o ensaio. Preserve protocolo,
manifesto, rótulos, configuração, decisões e relatório por revisão, com hashes.
Não inclua tokens, endereços com credenciais ou imagens em relatórios públicos.
Não sobrescreva um ensaio reprovado.

O manifesto precisa identificar:

| Grupo | Conteúdo obrigatório |
| --- | --- |
| Autoridade e escopo | Responsável, fontes autorizadas, datas, uso pretendido, classes e exclusões; nenhum acionamento automático durante observação |
| Software | Commit de núcleo/extensões/interface/integração, versões de dependências, schema dos dados e configuração completa do pipeline |
| Execução | Sistema, processador, memória, acelerador, provedor efetivamente usado, número de sensores concorrentes e carga de fundo |
| Imagem | Fonte, resolução, codec/ingestão, cadência configurada e recebida, região, preparo, calibração, posição/zoom e alinhamento aplicáveis |
| Modelo e IA | Origem/licença/hash dos pesos e manifesto, normalização, dimensão, revisão; perfil/provedor/modelo de IA, prazo, alternativas, permissão de imagens e cotas |
| Decisão | Identificadores de classes, descrições, referências, limiares, margens, permanência por classe, confirmações e validade |
| Relógios | Base monotônica local, timestamp físico disponível ou ausente, sincronização entre máquinas, erro máximo e método de medição |
| Consumidor | Versão e configuração do Home Assistant, mapeamento, frequência de consulta, expiração e nenhum serviço de atuação conectado |
| Critérios | Metas por direção, iluminação, duração de evento, cobertura, erro, falso estado por hora, latência e carga sustentável |

Os identificadores devem ser opacos. Descreva a relação com o local real apenas
no registro privado autorizado. Separe o arquivo de ambiente de segredos operacionais.
Um hash do manifesto registra uma declaração; o produto não verifica automaticamente
que o hardware ou o provedor descrito foi o realmente utilizado.

## 2. Dados independentes e verdade de referência

Separe **referências**, **calibração** e **teste reservado** por sessão e aparência
antes de executar modelos. Quadros vizinhos, recortes e transformações derivados
do mesmo original ficam na mesma partição. Registre ancestrais e hashes para
detectar vazamento. Uma sugestão aceita como referência sai da avaliação independente.

O usuário configura poucas fotos por classe. O conjunto maior de engenharia deve
cobrir o domínio pretendido, sem impor milhares de capturas ao usuário final.
Não substitua imagens reais independentes por dados sintéticos para aprovar qualidade.

Um humano ou instrumento independente rotula estado observável, transição e perda
de observabilidade. A saída do próprio modelo ou a “certeza” da IA não é rótulo.
Revise discordâncias antes de calcular métricas, preservando os rótulos originais
e a justificativa da correção. Não consulte o resultado do modelo para escolher
qual interpretação tornará o ensaio aprovado.

Para cada sequência, registre sessão, iluminação, duração, hash, região/preparo,
intervalos sem lacunas nem sobreposição e transições por direção. Uma transição
manual imprecisa recebe instante possível mais cedo e mais tarde. Trechos com
oclusão ou estado impossível de distinguir recebem rótulo **não observável**;
não são convertidos para a classe negativa nem removidos silenciosamente.

O contrato de replay existente está em
[`tests/visual_state_corpus.py`](../../tests/visual_state_corpus.py). Ativos de replay
do comparador usam `frames.npy` e `timestamps_ms.npy` em um arquivo NPZ, com hashes
e limites verificados pelo runner. Converter uma gravação exige preservar origem,
tempos e parentesco; alterar pixels não cria uma sessão independente.

## 3. Ensaio gradual, somente de observação

1. **Pré-condições:** confirmar que os testes locais pertinentes passaram, que
   existe backup recuperável e que o destino não está ligado a atuação. Conferir
   referências, região, relógios e configuração efetivamente salva. Se isso não
   puder ser demonstrado, não iniciar campo.
2. **Triagem:** observar sessões separadas de dia e noite, incluindo a troca para
   infravermelho quando aplicável. Um piloto com 20 transições por direção e
   observação por alguns dias ajuda a encontrar falhas grosseiras; não comprova
   sozinho 99% de acerto ou uma taxa rara de falso estado.
3. **Calibração:** avaliar poucas referências diversas, limiares de entrada/manutenção,
   margem e cadência. Registrar hipóteses e políticas comparadas. Não usar o teste
   reservado para escolher o melhor resultado.
4. **Congelamento:** fixar configuração, pesos, exemplos, protocolo e partições por
   hash. Definir antes do teste as condições sustentadas e o critério estatístico
   de cada alegação. Condição sem meta ou amostragem suficiente permanece inconclusiva.
5. **Avaliação reservada:** executar uma vez por versão congelada, incluindo todas
   as condições planejadas e falhas. Registrar também eventos perdidos, timeouts,
   amostras descartadas e períodos sem conclusão. Não relatar só decisões concluídas.
6. **Revisão:** comparar métricas e invariantes com o contrato. Correção orientada
   pelo conjunto reservado torna esse conjunto regressão conhecida; será necessária
   nova evidência independente para nova alegação de qualidade.

Qualquer movimento físico usado para gerar uma transição pertence a um procedimento
humano autorizado separado. Não fechar portões sobre objetos, entrar em zonas de
risco ou retirar proteções para testar o classificador. Falhas de rede, processo e
credenciais devem ser induzidas primeiro em ambiente descartável.

## 4. Condições e resultados obrigatórios

| Condição | O que observar |
| --- | --- |
| Dia, noite, infravermelho, crepúsculo e luz artificial | Sem troca indevida causada somente pela iluminação; métricas separadas por condição |
| Mudança lenta sem movimento detectado ou evento ONVIF | Reconciliação recebe imagem nova e respeita o orçamento de atraso |
| Transição breve e no limite da cadência | Menor duração detectável declarada; perdas contabilizadas, sem promessa universal |
| Classe visualmente parecida, cena nova e três ou mais classes | Abstenção quando margem insuficiente; candidato de uma classe não herda votos de outra |
| Região fora do campo, parcialmente visível ou alinhamento recusado | Indisponibilidade ou recusa; nunca estado negativo por ausência de pixels |
| Oclusão, lente coberta, reflexo e saturação | Recusa quando a distinção deixa de ser observável; não aceitar geometria válida como prova suficiente |
| Retorno da região, reinício e restauração | Nova confirmação com capturas posteriores; histórico e buffer antigo não viram estado atual |
| Quadro repetido, fora de ordem, timestamp futuro ou geração antiga | Não duplica voto nem renova validade |
| Silêncio, perda de rede, fonte parada ou inferência bloqueada | Runtime e receptor expiram independentemente; indicação operacional suspensa |
| IA lenta, cota cheia, resposta inválida ou divergente | Prazo global e cotas respeitados; resposta tardia não substitui evidência mais nova |
| Sugestão de IA errada | Não participa até revisão; correção e desfazer verificáveis |
| Renomear, adicionar, remover ou desativar classe | Mapeamento por identificador preservado; classe não mapeada não vira falso |
| Alterar modelo, referências, preparo ou política | Indicação histórica anterior suspensa; requer revisão da nova combinação |
| Carga crescente, modelo frio/quente e cancelamento | Filas e memória limitadas; vaga nativa retida até o trabalho terminar |

Uma falha não desaparece do relatório por ter sido difícil de reproduzir. Marque
**reprovado** quando o resultado contradiz o contrato e **inconclusivo** quando a
coleta ou o tamanho da amostra não permite decidir. **Não ensaiado** é outra situação.

## 5. Medir atraso e duração sem esconder perdas

Registre mudança física quando conhecida, exposição/captura, recepção, admissão,
preparo, espera por recurso, inferência, primeira evidência candidata, confirmação,
leitura do endpoint e mudança da entidade no Home Assistant.

Use diferenças de relógio monotônico para duração local. Só compare timestamps de
máquinas distintas com sincronização e incerteza documentadas. Se a câmera não
oferecer timestamp físico validado, o resultado é **recepção até decisão**, não
**mudança física até decisão**. O atraso da câmera fica não medido até um ensaio
externo autorizado com referência temporal independente.

Se a transição aconteceu entre `t_inicial` e `t_final` e foi confirmada em `t_decisão`,
seu atraso fica entre `t_decisão - t_final` e `t_decisão - t_inicial`. Não transforme
esse intervalo em um ponto de precisão falsa. Resultado anterior ao limite inicial
precisa de investigação como possível antecipação incorreta. Para provar atendimento
ao prazo, use o limite superior, incorporando o erro conhecido dos relógios.

Meça o total diretamente. Não some percentis de parcelas sobrepostas. Publique
denominadores, contagem de falhas/perdas e proporção de oportunidades concluídas
dentro do prazo, além dos percentis das conclusões. Uma transição sem conclusão não
pode desaparecer de um relatório de latência favorável. Histograma de telemetria
com faixas de 100 milissegundos não comprova um limite menor que sua resolução.

| Perfil responsivo local: metas de engenharia a demonstrar | Limite |
| --- | --- |
| Confirmação local, sem permanência adicional por classe | Percentil 95 até 1 segundo; percentil 99 até 2 segundos |
| Aplicação na entidade do Home Assistant | Percentil 95 até 2 segundos; percentil 99 até 3 segundos |
| Expiração no runtime | Validade contratada mais até 100 milissegundos de despacho |
| Expiração no receptor | Validade remanescente descontada mais até 250 milissegundos de despacho |
| Retenção após contradição inconclusiva | Até 500 milissegundos, sem ultrapassar a validade anterior |

Essas metas dependem de observabilidade, duas confirmações, hardware, ingestão e
carga declarados. Para IA direta, congele um perfil e orçamento próprios compatíveis
com a latência observada. Não atribua automaticamente a ela as metas locais nem
alongue validade apenas para mascarar atraso. Sob saturação, a obrigação é limitar
recursos e expirar corretamente; a capacidade responsiva só inclui a carga sustentada.

## 6. Métricas de qualidade e quantidade de evidência

Relate por classe, direção de transição, iluminação e sessão:

- Transições corretas e perdidas, matriz de confusão, falsas confirmações por hora
  observável, duração de cada falso estado e oscilações sem mudança física.
- Tempo conclusivo dividido pelo tempo observável; tempo inconclusivo em região
  válida; tempo indisponível por motivo e recusa correta de trechos não observáveis.
- Tempo indevidamente mostrado como classe negativa após perda de evidência.
- Uso e custo de IA, chamadas por evento, alternativas tentadas e benefício de
  exemplos revisados avaliado em sessões independentes.
- Latência, descartes, filas, memória máxima, uso de processador/acelerador e carga
  sustentável. Relate execução fria separada de execução aquecida.

O candidato de produto tem metas iniciais de **99% de transições corretas** e
**95% do tempo observável conclusivo**, conjuntamente. Aumentar abstenção para
melhorar acerto pode reprovar cobertura. Um perfil só vale para as classes,
condições e número de sensores declarados. Congele também o orçamento admissível
de falso estado por direção e por hora para o uso específico; sem ele, não declare
uma instalação complementar em alarme qualificada.

Relate estimativas e intervalos de confiança; não confunda atingir 99% na amostra
com demonstrar pelo menos 99% com confiança estatística. Se essa segunda alegação
for exigida pelo contrato, o limite inferior unilateral de confiança também precisa
atingir a meta. Falta de evidência implica resultado inconclusivo, não relaxamento
posterior do critério.

Para zero erros em `n` oportunidades independentes, resolver `(1 - p)^n = 0,05`
produz limite superior unilateral de 95% `p = 1 - 0,05^(1/n)`. Em 300 oportunidades,
esse limite é aproximadamente 0,994%; em 3.000, aproximadamente 0,100%. Trata-se do
modelo binomial exato, não de independência garantida pelas imagens. Consulte
[NIST: limites binomiais exatos](https://www.itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbici.htm).
Com erros observados, calcule o intervalo binomial correspondente; não aplique a
fórmula de zero erros. Quadros vizinhos não são oportunidades independentes.
Para métricas temporais correlacionadas, preserve o agrupamento por sessão na
análise de incerteza e declare quando há poucas sessões para estimá-la.

Zero falsas ocorrências em `T` horas, sob hipótese explícita de taxa Poisson
estacionária, dá limite unilateral de 95% `-ln(0,05)/T`, cerca de `3/T`. Isso é uma
derivação da probabilidade de zero ocorrências, não prova de que a hipótese se
aplica à instalação. Sete dias sem ocorrência não demonstram menos de uma por
semana: o limite ainda fica próximo de três por semana. Clima e iluminação podem
quebrar a estacionariedade. Não converta um piloto curto em promessa anual.

## 7. Executar regressões e replay sem acessar a instalação

Use diretório de teste externo, ambiente local isolado e modelos/dados explicitamente
permitidos. Os testes de navegador têm perfil próprio:
[`playwright.visual-state.config.js`](../../playwright.visual-state.config.js).
Não reutilize a configuração geral de testes contra uma instalação real.

O [runner de modelos](../../tests/benchmarks/visual_state_models.py) exige hashes
de protocolo, corpus e manifesto; a avaliação reservada exige o relatório de
calibração correspondente. Consulte suas opções sem abrir câmeras:

```sh
uv run --no-sync python tests/benchmarks/visual_state_models.py --help
```

O runner mede inferência e replay permitido, mas não substitui a medição integrada
da ingestão e do consumidor. Use o procedimento Linux fixado por imagem no
[README do Home Assistant](../../integrations/home_assistant/README.md) para o
consumidor descartável. Não aponte esses testes para o Home Assistant da casa.

Ensaios de 1, 4 e 8 sensores são pontos de investigação de capacidade, não promessa
de suporte a oito. Aumente a carga somente dentro do ambiente autorizado e após
verificar memória, expiração e filas. Preserve dados rejeitados e a hipótese da
próxima tentativa; nunca enfraqueça um validador para obter aprovação.

## 8. Decisão, auditoria e reversão

O relatório deve conter revisão/ambiente, protocolo congelado, partições, contagens,
métricas com incerteza, falhas, condições não cobertas, decisão e responsável pela
revisão independente. Separe aprovação de mecanismo local e qualificação física.
Uma violação de validade, desconhecimento ou mapeamento bloqueia uso operacional
mesmo quando as métricas médias parecem boas.

Os registros locais de evidência aceitam somente `laboratory`, `replay` e
`isolated_consumer`; não há escopo de aprovação física nessa API. Após revisar o
relatório, registre os hashes pela operação documentada no
[README Vision](../../extensions/vision/README.md). Um teste rápido não registra
aprovação. O painel distingue compatibilidade do relatório e suspensão operacional
no instante da consulta; nenhuma delas autoriza ativação.

Mudanças de câmera, geometria, modelo, política, exemplos, ambiente ou consumidor
exigem revisar quais ensaios precisam ser repetidos. A invalidação automática é
conservadora para configuração/arquivos e não detecta todas as mudanças físicas
ou de ambiente. Também não deve ser confundida com o estado atual do sensor.

Se o ensaio reprovar, mantenha uso informativo ou desativado conforme o contrato.
Para reversão, desative o fluxo, confirme indisponibilidade no receptor, preserve
os dados rejeitados, restaure a revisão conhecida e suas referências, confira
origem/modelo e inicie sem valor até novas confirmações. Publicação restaurada
fica desativada. Registre o resultado dessa recuperação; restaurar arquivos não
restaura uma aprovação anterior. A liberação de campo identifica explicitamente
quais instalações e condições foram revisadas, com autorização própria separada.
