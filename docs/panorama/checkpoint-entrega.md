# Panorâmica ao vivo — seguimento móvel em validação, 20/10/2026

## Continuidade da reprodução MSE — 22/10/2026

O serviço MSE preserva a porta alternativa já vinculada enquanto a porta
solicitada não mudar. Consultas simultâneas de reprodução não devem reiniciar
o serviço por confundir sua própria porta com um conflito. Alterações reais
de fontes ou de porta continuam sendo aplicadas. A regressão cobre dois
clientes concorrentes, mudança de fontes, mudança de porta e nova partida.
Essa correção de disponibilidade não altera cobertura, calibração ou controle físico.

## Preparação de atenção — reaproveitamento de descoberta, 20/10/2026

A atenção panorâmica pode reutilizar metadados ONVIF recentes de uma operação
anterior já parada e fechada, por até 30 segundos contados da descoberta original.
O vínculo exige mesma concessão, fence, fonte, mapeamento e geometria. Configuração,
pose, automação nativa, posse e imagem parada continuam sendo verificadas por operação.
Nenhum consumidor de captura, quadro ou recibo de parada é transferido. Falhas
descartam o reaproveitamento; a telemetria o identifica por `discovery_reused`.
Essa redução de descoberta repetida tem cobertura de software, mas ainda não
comprova ganho físico de latência, seguimento móvel, cobertura ou precisão.

## Atenção física e ensaio local supervisionado — 20/10/2026

Foi acrescentado um percurso explícito de qualificação da atenção, com ida e
retorno observados sob a mesma posse de controle. O registro exige movimento
real, vínculo exato com a referência, chegada até 12 pixels, retorno até 3 pixels
e parada confirmada; leituras e testes sintéticos não produzem qualificação.

O piloto da Frente completou um percurso real: chegada com erro de 11,37 pixels
em 29,99 segundos e retorno com 0,44 pixel em 15,86 segundos, na análise de largura
960. Houve tentativas anteriores reprovadas, preservadas fora do repositório.
Uma rodada aprovada não comprova repetibilidade, enquadramento da lente Zoom nem
acompanhamento de pessoas e veículos em movimento. A latência observada ainda
é inadequada para passagem rápida.

O modo panorâmico físico agora recebe atualizações qualificadas do alvo durante
a aproximação. Observações vencidas interrompem correções; observações novas
podem reiniciar a confirmação após perda temporária, sem reabrir CLOSE explícito.
A ausência de resposta só permite ampliar sondagens após observação causal
de imobilidade. Respostas com atraso inicial são tratadas separadamente por
sentido, sem converter duração em ângulo nem reduzir tolerâncias.

Os ensaios de integração também identificaram atualização ausente do estado
ONVIF após a parada visual e evidência de chegada anterior à limpeza. A execução
passa a confirmar ambos e medir o alvo na imagem final parada. Seguimento móvel,
latência ponta a ponta e conservação do rastro mundial permanecem em validação
física; este checkpoint não declara sua aprovação.

A atenção passa a compartilhar com a panorâmica ao vivo a verificação de chegada
recente sob o mesmo Stop. Quando o quadro parado ainda é o da chegada, sua idade
é de até um segundo, a posse continua válida e o estado físico ONVIF é confirmado,
essa evidência pode ser entregue sem repetir Stop, janela de estabilização e
localização. A consulta de estado não renova a idade do quadro: imagem e comando
são conferidos novamente depois dela. Retorno continua exigindo até 3 pixels;
chegada, até 12. Qualquer perda dessas condições mantém a parada e a observação
completas. O traço informa `final_stop_reused`; redução da latência física ainda
precisa ser medida em campanha, sem extrapolar o resultado dos testes de software.

O diagnóstico de atenção registra `preparation_elapsed_ms`: marcos cumulativos
medidos por relógio monotônico na mesma operação, desde a preparação local até
reserva, descoberta, aquisição, parada, referência, pose e localização. Diferenças
entre marcos consecutivos medem as etapas; horários civis permanecem apenas para
correlação. Falha mantém os marcos já concluídos, sem inventar os seguintes. Esses
tempos não incluem espera anterior pelo controlador/referência nem certificam
o instante físico de exposição da imagem.

Passagens reais posteriores produziram pequenas correções mecânicas, mas não
mantiveram ciclista e pessoa em movimento centralizados. A lente Zoom mostrou
o alvo cortado na borda. Um percurso curto adicional passou com chegada de
5,81 pixels em 2,82 segundos e retorno de 1,56 pixel em 5,12 segundos; outro
percurso maior reprovou por divergência no retorno. Não há aprovação geral de
repetibilidade nem de seguimento móvel.

Foram corrigidos encerramento lento do decodificador, recusa de eventos fora da
região interrompendo o controlador, perda da posição válida durante a espera por
reaparecimento e substituição enganosa da causa de falha por reinício. A resposta
do motor não é recalculada a partir de deslocamento abaixo da resolução visual
nem transferida de uma velocidade reduzida para a velocidade normal. Respostas
de operações aprovadas podem ser reutilizadas em memória por até cinco minutos,
sob vínculo exato e limite de mudança de orientação, sem renovar sua validade por
simples reutilização. A preparação e o histórico recente de navegação são
registrados separadamente da confirmação de chegada.
Uma única ultrapassagem pequena do alvo pode receber correção, com localização
recente e dentro de 12 pixels; repetição ou afastamento continua interrompendo a
operação. Isso não altera o limite de 3 pixels para aceitar o retorno.

Testes sintéticos percorrem projeção, identidade e histórico mundial de pessoa e
carro durante rotação da câmera; inspeção de quadro com pose visual qualificada
não depende de posição numérica ONVIF estável. Isso não certifica o rastro completo
de uma notificação real nem a calibração cruzada da lente Zoom.

## Diagnóstico da atenção por eventos — 20/10/2026

A atenção simulada preserva o alvo solicitado e distingue recusa pela região,
referência indisponível, vínculo incompatível e qualificação física pendente.
Um evento observado inicialmente recusado pode ser reavaliado em atualizações
válidas; seu encerramento conserva o ciclo de vida. A confirmação de seguimento
simulado continua durante mudanças do raio na mesma referência panorâmica.

Relações espaciais aceitam a evidência geométrica de ambos os participantes na
mesma referência panorâmica e revisão, sem fabricar confiança estatística.
Localizações visuais precisam corresponder à captura individual; referências
incompatíveis e confiança explicitamente baixa continuam sem alvo mundial
qualificado. O teste integrado percorre mapeamento, rastreamento, agrupamento e
relação, além dos casos de rejeição.

A telemetria separa evento até decisão de comando até chegada verificada na fonte
selecionada. Simulação não publica latência de chegada física. Uma chegada na lente
Wide não qualifica enquadramento na lente Zoom. Estas mudanças não promovem
qualificações, não habilitam seguimento mecânico contínuo e não alteram os critérios
de reconstrução, cobertura, apontamento ou retorno.

## Edição de regiões de interesse — 12/10/2026

A assinatura de composição versão 2 preserva a calibração ao editar vértices
e centro no plano das áreas de interesse. Câmera, fonte, referências da cena,
elevação e geometria da área fora desse plano continuam protegidas. Remover
ou substituir elementos permanece uma mudança incompatível. A revisão integral
do mapa continua disponível às regras de área, independentemente desse vínculo.

Registros antigos recebem o vínculo novo somente quando a assinatura anterior,
a fonte e a referência salva ainda correspondem ao contexto atual. A migração
persiste o vínculo sem recalcular solução ou transferir verificações físicas.
Testes de API e projeção conferem preservação e bloqueios; não qualificam precisão,
cobertura ou apontamento físico. Notificações conservam trajetórias separadas
por sujeito, com lacunas explícitas, e enquadram evidências nas três vistas.
Falhas recuperadas antes da publicação conservam seus motivos no histórico
e no diagnóstico da notificação. Resize reenquadra uma seleção ainda não
navegada manualmente; os buffers de trajetória 3D são reutilizados. O orçamento
de transporte omite primeiro imagens temporárias e solicita atualização por
HTTP para metadados maiores, preservando a evidência armazenada.


## Referência estática por fonte — 12/10/2026

Fontes de vídeo podem publicar uma imagem única com geometria plana pelo serviço
de captura compartilhada, sem controle de movimento ou reconstrução esférica.
Recorte, quatro correspondências, revisão ativa e projeção da imagem salva usam
os mesmos recursos do mapeamento panorâmico. A fonte com movimento pode escolher
essa alternativa explicitamente; sua panorâmica ampliada continua disponível.

Cobertura integral significa apenas o quadro capturado. Referências estáticas
não habilitam apontamento ou navegação física. Detecções de fontes fixas exigem
identidade do quadro e geometria válida; uma fonte marcada como móvel exige
verificação visual do enquadramento para projeção ao vivo. Validação sintética
não qualifica dispositivo, precisão física nem cobertura além da imagem.

## Suspensão do processamento entre observações — 11/10/2026

A fonte pode liberar sua captura entre janelas ONVIF, preservando demanda visual
de sujeitos recentes antes da confirmação de uma ocorrência. Assim, candidatos
e veículos parados continuam sendo detectados e mapeados enquanto observados;
encerramento e expiração permitem suspender ambas as etapas. Falhas espaciais
mantêm seus motivos e não recebem coordenadas fabricadas. Essa redução não muda
calibração, geometria ou proteção de comandos. Testes de ciclo de vida e consumo
em intervalo inativo não qualificam cobertura ONVIF nem ausência de perdas físicas.

## Acesso ao mapeamento na configuração — 10/10/2026

A panorâmica pertence aos detalhes da fonte selecionada. A seção de mapeamento
oferece ações separadas para abrir a composição e abrir o mapeamento do elemento;
quando há vários elementos da câmera, a escolha é explícita. A abertura respeita
alterações não salvas e não inicia aquisição nem movimentação. Os estados de
imagem, compatibilidade, cobertura e controle continuam independentes. O novo
acesso não modifica a geometria nem os critérios de aceite físico.


## Validação visual do mapeamento — 10/10/2026

O fluxo atual substitui os seis ajustes e duas conferências descritos no histórico
abaixo. Quatro pares utilizáveis iniciam a projeção; todos os controles completos
participam do ajuste. Divergências geram avisos e a pessoa aceita explicitamente
a revisão que atende ao seu uso. A prévia de edição não persiste nem ativa dados.

Ghost points acompanham hover e consultas por clique, toque e teclado. O alvo
fixado permanece durante navegação e refinamento. Rotação muda apenas a vista.
Posições extrapoladas continuam na interface e no pipeline, identificadas por
observação; raios sem interseção não recebem coordenadas fabricadas. Textura no
chão conserva extensão finita e recortes de apresentação.

Aceite visual não habilita apontamento físico. Verificações operacionais de
posição, fonte, revisão e suporte para comandos continuam separadas. Testes
sintéticos de software não qualificam precisão física, cobertura, reconstrução
ou retorno mecânico. Os resultados físicos abaixo conservam seus limites.


## Ajuste contínuo do mapeamento — 10/10/2026

O mapeamento usa uma única tela, com ambas as vistas e ativação direta. Selecionar
um local define a edição do par; concluir sua seleção retorna à consulta sem
apagar o rascunho. Mensagens de previsão têm espaço próprio abaixo das vistas.
Alvos fixados podem ser centralizados explicitamente em cada painel, mantendo
zoom e rotação. A atualização do alvo na composição acompanha o desenho da vista
sem alterar sua área de rolagem. Estas mudanças substituem o wizard e a alternância
de vistas descritos nas entradas históricas abaixo; não mudam a geometria ou os
requisitos físicos.


## Usabilidade do mapeamento — 08/10/2026

Clique e toque curto marcam os dois lados do par; uma única confirmação salva.
Arrastar sobre um marcador usa a navegação da imagem, sem selecionar outro par.
No celular, panorâmica e composição têm alternância explícita e indicam qual
lado já foi marcado. Confirmar e cancelar permanecem fora da rolagem.

A revisão mantém os mapas visíveis, enquadrados e sem edição dos pontos.
Ativar mapeamento é uma ação explícita; editar pontos preserva a versão ativa.
Os textos distinguem rascunho salvo, mapa ativo e retorno às opções da câmera.
Essas mudanças de interface não ampliam o aceite físico documentado abaixo.

## Mapeamento de panorâmica salva na composição — 07/10/2026

A preparação da imagem fica na câmera; a composição abre o wizard de seis
pares de ajuste e dois de conferência. Sem imagem compatível, a entrada leva
à câmera e conserva o contexto de retorno. Revisão do mapa, correção dos
pares e restrição por área não exigem apontamento físico.

Novas aquisições registram uma identidade geométrica versionada, separada
da proteção operacional de captura. Mudanças de taxa de quadros, ingestão,
domínio de relógio e endereço de eventos não invalidam essa geometria.
Fonte, montagem, resolução e codec continuam significativos. Artefatos antigos
sem essa proveniência não são migrados nem declarados compatíveis por inferência.

As vistas 360 em duas e três dimensões projetam a panorâmica salva somente no
suporte validado, respeitando sua máscara, recorte de apresentação e área da
composição. A amostragem usa raios esféricos; a imagem salva é identificada
como tal e não representa vídeo ao vivo.

Ensaios locais sintéticos demonstraram oito pares, ativação, recuperação de
edição, conflito, teclado, toque e aplicação/remoção do recorte nos pixels
renderizados das duas vistas. Essas evidências não qualificam cobertura total,
precisão física, captura ou retorno mecânico. O retorno físico continua com
o estado documentado abaixo; esta mudança não altera seus critérios de aceite.

## Resposta temporal e retorno — 04/10/2026

Na revisão carregada `6a780f1b`, uma saída horizontal nominal de 0,025 por
120 milissegundos deslocou 13,19 pixels. O único retorno reservado ficou em
4,03 pixels, acima do limite de três na análise de 960×540. A estática final
foi de 0,036 pixel, com correspondências distribuídas e sobreposição de 99,29%.
O desvio já estava estável antes do Stop; não houve pulso corretivo ou repetição.

Stop e estado parado foram confirmados, a configuração nativa foi restaurada
exatamente e os presets anteriores foram preservados. A referência própria
foi conservada porque o retorno óptico reprovou. Iluminação e horário diferem
do ensaio histórico de 200 milissegundos: não há ganho mecânico proporcional
ou repetibilidade qualificados. Cobertura e acompanhamento físico dependentes
do retorno preciso continuam sem aprovação.

A remoção HTTP de preset agora aceita o nome esperado e reutiliza a proteção
de identidade existente no serviço; divergência recusa a mutação. Essa proteção
de cadastro não certifica a posição física da câmera.

## Trajetória de retorno observada — 03/10/2026

Ensaio pontual na Frente, com revisão carregada `ce0b2e87`, referência nova e
presets anteriores preservados: retorno reprovado em 9,23 pixels contra o limite
existente de três, no espaço de análise 960×540. O par final estático apresentou
0,038 pixel. A posição nativa voltou ao mesmo valor inicial; isso não comprovou
igualdade do enquadramento.

O desvio já estava estável em quatro observações antes do Stop, solicitado
6,108 segundos após o início do retorno. A espera fixa de dois segundos não é,
portanto, explicação suficiente para esta falha. Folga, quantização e posição
do eixo não reportado permanecem hipóteses. Nenhuma correção ou repetição foi
executada; Stop foi confirmado e a configuração nativa restaurada exatamente.
Cobertura e acompanhamento físico dependentes do retorno preciso continuam
sem aprovação. Novo ensaio exige diagnóstico ou variável relevante nova,
preservando os critérios e limites.

## Integridade da referência de retorno — 03/10/2026

A atualização do catálogo invalida a indicação de preset pendente ou ativo quando
seu token desaparece ou passa a outro nome. Repetir a criação com a mesma chave
também recusa um token cujo nome já diverge. Falha ao consultar o catálogo não é
tratada como remoção. O número do token e o último comando aceito não comprovam
a identidade física de uma referência de retorno.

O retorno preciso continua sem aprovação no ensaio com erro de 6,75 pixels e
limite de três pixels. Uma próxima coleta exige referência atual conferida,
controle exclusivo, revisão carregada conhecida e observação da trajetória e da
estabilidade. A espera fixa de dois segundos antes do Stop é uma hipótese a
investigar; a causa mecânica ou temporal da reprovação não está confirmada.

## Integração local — 26/09/2026

Base `0b162eb5`, sem novas capturas ou movimentos físicos. A verificação local de cobertura e sessão live encontrou 213 testes aprovados e duas falhas de publicação regional: uma reconstrução aprovada substituía o ativo, inclusive quando sua identidade óptica estava antiga. Corrigida a publicação v4 para conservar o novo resultado como candidato quando já existe geometria ativa. Reabertura e a finalização automática do cliente também preservam o ativo; a primeira publicação continua disponível. Os arquivos integrais e máscaras anteriores permanecem intactos. A regressão de API/publicação passou em 283 testes; o cenário foi ampliado para identidade atual/antiga e retorno confirmado/não confirmado.

Na atenção PTZ, observações vencidas não iniciam nem prolongam atenção; observações atrasadas conservam somente sua validade restante. Resolução lenta é reavaliada antes da aquisição de controle. Teste anterior reproduziu `FOCUSED` indevido com observação de seis segundos e timeout de cinco. Aprovação local: 40 testes do controlador, 39 de integração API/pipeline e quatro de modelos/persistência. Evidência simulada não qualifica acompanhamento mecânico contínuo.

Revisão da integração encontrou que a proteção automática precisava preservar também uma adoção deliberada: a interface agora permite comparar a imagem atual com a candidata e escolher **Usar esta panorâmica**. A seleção exige identidade e revisão esperadas, recusa conflitos, mantém a anterior e não recalibra mapeamentos. Controles de preparação ligados à imagem ativa ficam ocultos durante a revisão da candidata. Doze casos focados de API/cobertura, traduções, tipos e build aprovados; o cenário de navegador usa aplicação renderizada e imagens/transporte sintéticos, com inspeção desktop e móvel. A seleção não inicia movimento.

Aceites físicos de cobertura, rapidez de chegada/retarget e recuperação prolongada continuam **não alcançados nesta rodada**. Os resultados históricos permanecem com seus limites originais; nenhuma autorização de movimento foi presumida. A próxima prova física depende de autorização específica e cenário congelado; outras frentes da integração seguem independentes.

## Histórico anterior

Ordenação de chegada também exercitada no Chromium isolado: a função real de aceitação preserva o indicador após resposta atrasada, aceita a próxima sequência e respeita bloqueio prioritário. Nenhuma requisição de rede; navegador encerrado após o teste. A apresentação e o registro são fixtures, portanto esta prova não cobre o componente completo, a extensão servida ou movimento físico. Validação prática com o pacote recompilado continua pendente.

Validação adicional reutiliza a montagem existente de LiveSession React com renderer real, imagens sintéticas e transporte simulado: respostas tardias de sucesso e falha não desfazem chegada confirmada por consulta nem introduzem alerta indevido. Arquivo de integração aprovado após inclusão dos cenários; 47 testes locais haviam passado antes dessa ampliação. Pacote da extensão recompilado. Viewport, player, geometria observada e câmera permanecem simulados neste teste; aceite físico não é ampliado.

Observação final da versão atual: três minutos, 359/359 amostras alinhadas, ambos vídeos prontos e avançando 2652 quadros cada; nenhuma reconexão observada, erro da página ou comando físico. Amostragem de cerca de 500 milissegundos não prova todos os intervalos. Rodada encerrada antes do prazo, serviços próprios liberados; aceite de velocidade e condições adversas permanece pendente. Guia 28.13.52–53.

Corrigida perda da evidência de cadência quando o stream para entre coletas e framesPerSecond desaparece: contador de quadros decodificados pode qualificar a taxa; ausência/reset continua conservador. 20 testes de recuperação, tipos e build de produção aprovados. Nova fase de falha real recuperou vídeo em 6,580 segundos e projeção em 7,086; variação confirma que o alvo típico ainda não está aprovado. Guia 28.13.51.

Primeira reconexão de stream estabelecido agora pode começar imediatamente; tentativas posteriores preservam atraso progressivo. Falha real: espera até nova negociação caiu 1,056→0,160 segundo; vídeo útil em 5,567 segundos e projeção em 6,074. Alvo de 5 segundos ainda pendente. 17 testes de recuperação e tipos aprovados; reabertura sem instrumento teve 20/20 amostras alinhadas. Guia 28.13.50.

Recursos medidos: tempo de tarefas da página caiu cerca de 95,9% em aba realmente oculta (0,581→0,024 segundo em janelas de aproximadamente 11 segundos), com players descarregados. Não equivale a CPU total. Seis trocas de câmera encerraram as sessões antigas (410), mantendo um/dois players e um documento; heap após coleta explícita ficou em 14,25–14,57 MB, sem certificação de vazamento a longo prazo. Guia 28.13.49.

Garagem validada sem movimento na mesma interface: vídeo em 2,064 segundos, alinhamento em 3,069; 54/54 amostras alinhadas após a inicialização. Um player e PiP corretamente omitido por ausência de fonte secundária. Troca de volta à Frente encerrou a sessão anterior (410) e relocalizou sem comandos. Prova de visualização em outra câmera, não de apontamento físico da Garagem; guia 28.13.47.

Hipótese de paralelismo nas verificações anteriores ao preset foi testada e retirada por não demonstrar ganho: custo até recibo permaneceu aproximadamente 0,55 segundo. Ensaio de retorno 19,489 graus terminou em 7,447 segundos, erro 0,888 pixel. Versão anterior restaurada, 147 testes de captura aprovados; detalhes e comparação limitada no guia 28.13.46. Cota vigente 355/424, saldo 69.

Recuperação do player compartilhado agora também observa falta simultânea de pacotes e avanço da reprodução, com tolerância à taxa de quadros e sem novo timer. Falha WHEP real recuperou projeção em 6,833 segundos, contra 9,116 na comparação anterior; meta típica de 5 segundos ainda pendente. 25 testes direcionados e tipos aprovados; reabertura normal de 30 segundos teve 60/60 amostras alinhadas, sem reconexão adicional. Guia 28.13.44; nenhuma movimentação adicional.

Parar após mudança visual agora tem evidência causal: transição de movimento precedeu Stop por cancelamento, sem parada automática anterior. Encerramento observado 1,895 segundo, projeção 2,701 segundos; intenção cancelada sem chegada falsa. Gravação privada e diagnóstico em guia 28.13.43. Amostra atende teto de 2 segundos, sem certificar mediana de 1 segundo. Cota atual 354/424, saldo 70.

Parar na aproximação nativa cancelou a intenção em 1,816 segundo e recuperou projeção em 2,607 segundos, sem resultado ou pendência obsoletos. O Stop precedeu a parada automática, mas a transição visual só foi qualificada durante a limpeza posterior; não certifica ainda clique no botão após movimento visual previamente demonstrado. Dois ensaios custaram dois comandos: 353/424, saldo 71. Guia 28.13.42.

Consulta de sessão acelerada durante movimento, sem requisições sobrepostas e mantendo cadência reduzida em repouso/aba oculta. Ensaio real de 19,457 graus: controlador 6,461 segundos, projeção 6,927 segundos, erro central 0,777 pixel e um comando. Intervalo observado de consulta durante movimento: mediana 250 milissegundos. Esta amostra atende ao máximo de 7 segundos, mas não certifica a mediana de 4 segundos nem o aceite integral. Guia externo 28.13.41; cota 351/424, saldo 73.

Partida por referência nativa agora evita localização inicial duplicada quando há uma indicação recente, consumida uma única vez, mantendo parada observada e qualificação completa de chegada. Ensaio de 16,448 graus: 6,401 segundos no controlador, 7,749 até projeção, erro 1,072 pixel; rapidez ainda reprovada. Descoberta de capacidades antecipada e renovada durante observação ativa; posição, automação e configuração continuam sendo verificadas no clique. Três deslocamentos médios pelo clique normal com referências permanentes: **8,144 / 7,991 / 8,675 segundos**, mediana **8,144**, um comando por deslocamento e erros **3,390 / 0,194 / 2,984 pixels**. A descoberta/refresco no caminho crítico levou 0,28–0,33 segundo. Precisão aprovada nesses casos; rapidez em boa imagem continua reprovada (mediana 4, máximo 7 segundos). 238 testes de sessão/adaptador aprovados, sem substituição do aceite físico.

Dois pontos permanentes estão validados na Frente, separados de imagem/recorte/geometria e dos retornos temporários de aquisição. Persistência após reinício e um retarget nativo foram observados. A gestão de pontos está integrada ao painel de preparação, com catálogo, preparar, Parar e remover; build, tipos, traduções e inspeção visual aprovados. Preparo físico pela interface confirmado com dois movimentos e erro de 0,247 pixel; reabertura, remoção do ponto de ensaio e cancelamento com limpeza também observados. Causalidade/latência do botão Parar em pleno movimento ainda não certificada; regressões finais, recuperação e outras câmeras pendentes. Rodada ativa até 25/09 às 19:51:17 UTC; 355/424 comandos, saldo 69. Guia externo, seções 28.13.22–28.13.23; evidências privadas no diretório da tarefa. Nenhuma publicação nesta rodada.

Continuidade e visibilidade: 96,87 segundos com 378/378 amostras alinhadas e sem comandos durante navegação; aba realmente oculta e janela minimizada descarregaram ambos os players, com retorno alinhado em 1,513 e 1,262 segundos. Não quantifica economia de CPU nem exclui piscadas entre amostras. Falha real limitada à sessão WHEP principal recuperou quadros em 9,084 segundos e projeção em 9,337; teleobjetiva permaneceu contínua. Nova medição instrumentada levou 10,371 segundos; diagnóstico encontrou espera pelo próximo relógio após ICE disconnected. Correção no player compartilhado eliminou essa espera quando a ausência de progresso já estava comprovada: reação em 4 ms e projeção em 8,844 segundos na nova amostra. Oito testes de recuperação e typecheck aprovados. Ainda acima da meta de 5 segundos; detecção ICE levou quase 7 segundos. A comparação de quadros preservados com a referência na mesma perspectiva encontrou medianas de 0,7–1,5 pixel em pontos estáticos; as grandes bordas visuais não sustentaram erro geométrico global nem paralaxe dominante. Diferenças temporais de iluminação e veículos explicam parte importante da aparência; resíduos locais e regiões sem correspondências não estão universalmente certificados. Guia externo atualizado até 28.13.40.

**Meta vigente de rapidez:** tempos absolutos da seção 28.11 do guia externo substituem a redução percentual. Em boa imagem: medianas de 2/4/8 segundos para percursos curtos/médios/longos; tetos de 4/7/12. Em imagem degradada: medianas 5/8/12 e tetos 8/12/20. Medir clique até centralização observada e projeção atual de volta. As medições históricas de 29,266 segundos e de recuperação automática de 19,692 segundos não aprovavam essas metas; as medições atuais acima ainda não demonstram aceite. Cota inalterada.

## Histórico desta aceleração — estados e medições anteriores

Rodada encerrada dentro do prazo, serviços próprios e mídia desligados com readback. Nova janela é necessária para novos ensaios. Cota da rodada: quatro horas e 160 comandos adicionais para a Frente, até 25/09 às 15:50:32 UTC. Ledger acumulado 310/344, saldo 34. O usuário autorizou corrigir a geração e produzir novas panorâmicas/recortes quando necessário (seção 28.13 do guia), sem renovação implícita de cota. Sem mudança de firmware ou relaxamento das proteções.

Sentido de sondagem reutiliza dois movimentos originais concordantes e causalmente qualificados, próximos à orientação atual; não importa ganho antigo do motor. Medição que vence durante análise exige nova observação antes de aceitar chegada ou despachar. Leitura de JPEG passa a procurar marcadores de forma linear, preservando os bytes e todos os quadros.

Três percursos após essas correções: 29,266/23,461/29,370 segundos; mediana 29,266, 31,63% abaixo da referência histórica de 42,806 segundos. Erros de 5,575/4,694/9,994 pixels, mediana 5,575 contra 6,305 da referência. Retornos observados e sessões novas tornam os percursos aproximadamente comparáveis; não certificam posições mecanicamente idênticas nem uma distribuição da referência. A terceira chegada teve erro maior; preservar essa dispersão.

Falha tardia de WebRTC agora aciona recuperação compartilhada, e a sinalização tem timeout usando o helper existente. Reconectar renova explicitamente os players sem remontá-los durante movimento. Queda real inicialmente recuperou vídeo em 19,692 segundos. Monitoramento conjunto de ausência de quadros e conexão desconectada, reutilizando o intervalo existente, reduziu nova medição a 9,609 segundos; manual em 2,568. Dentro do teto de 10 segundos nessa amostra, mas acima da meta de 5 e sem repetição suficiente. Sinalização bloqueada recuperou pelo transporte alternativo em 8,897; retirada da falha e reconexão restauraram WebRTC em 4,004. Bloqueio interno de estatísticas/UDP do MediaMTX foi diagnosticado e permanece limitação separada: não houve atualização do binário nem reinício automático global.

Verificação: 165 testes de geometria/navegação, 128 de captura/saúde, doze de backend de captura e seis de recuperação WebRTC aprovados; typechecks e build de câmeras aprovados. Dois testes preexistentes de captura usam schema_version=1 e são recusados pelo modelo antes de atingir a captura; não contam como aprovação. Janela de cinco minutos: vídeos sem descarte e sem lacuna acima de um segundo (máximo 200,1 ms), mas duas de 299 amostras perderam a projeção por localização expirada após 3719 ms. Retarget real após recibo de movimento confirmou o último destino, erro 9,474 pixels, com oito comandos nas duas intenções e gravação inspecionada. Após redistribuir o paralelismo das mesmas comparações, nova janela de cinco minutos ficou 299/299 alinhada, sem recusa e sem lacuna acima de um segundo (máximo 198,1 ms; um quadro descartado na principal). O aceite integral continua pendente das metas absolutas e demais condições. Qualificação física conjunta comprovou movimento dos dois eixos, mas resposta diferente da soma isolada; nenhuma política automática combinada foi ativada.

Diagnóstico adicional do ciclo: 376 testes do executor aprovados após instrumentar durações, sem alterar decisões. Um pulso combinado de 0,3 segundo terminou parado e realinhado: leitura de estado após estabilidade 0,350 s, geometria 0,095 s, confirmação de quadros novos 0,272 s e gravação do replay 0,571 s. O ciclo total foi 3,874 s; não é tempo do clique completo. Próxima otimização deve preservar evidência e frescor, sem remover a confirmação visual. Preparação estática das referências será revisada na geração; o localizador já tem cache em memória, portanto não criar cache paralelo.

Gravação do replay mantém formato NPZ e todos os pixels, agora sem recompressão após Stop: benchmark alternado reduziu a mediana de 0,502 para 0,181 segundo; função real em 0,183. Arquivo cresceu de 18,8 para 27,5 MB, com limites de quadros/memória preservados. Dezessete testes de replay/diagnóstico aprovados. Backend de ensaio já usa a versão nova: quatro gravações reais em 237–284 ms. Ganho no clique completo ainda não medido.

Comparação física de velocidades 0,1/0,2 com pulsos de 0,3 segundo nos dois sentidos não mostrou ganho consistente: pan negativo 2,947/3,298 graus; positivo 2,386/2,033. Todas as operações paradas e localizadas, porém uma amostra por combinação e posições consecutivas não qualificam aumento automático. Velocidade do produto permanece inalterada.

Finalização ao vivo reutiliza a parada observada somente com mesmo quadro recente, recibo, comando, epoch, posse e configuração; falha, cancelamento ou prova vencida mantêm confirmação completa. 169 testes de sessão/câmera e 41 de parada/interrupção aprovados. Clique real: seis comandos, centro 11,608 pixels, parada reutilizada registrada e ambos os vídeos avançando após realinhamento. Controlador terminou em 21,607 segundos; alvo diferente das comparações anteriores e primeira reprojeção não cronometrada, portanto sem aceite de rapidez. Backend normal substituiu o instrumento privado.

Preparação da vista parada sobrepõe leitura de posição e localização, aguardando ambas e drenando a observação em falha/cancelamento. 150 testes de navegação/sessão aprovados. Ensaio curto classificado antes do despacho (3,732 graus): dois comandos, centro 11,148 pixels, controlador em 9,626 segundos e reprojeção confirmada em 10,790. A segunda janela inicial foi evitada, mas a meta de 2 segundos/teto 4 continua reprovada.

Evidências privadas em `~/.codex/task-artifacts/toposync/panorama-ao-vivo/aceleracao/retomada-ampliada-20260924/ciclos-20260925/`; guia externo específico, seções 28.10.3–28.10.7. Alterações locais em development, sem publicação nesta rodada.

## Histórico — comparação anterior negativa

A extensão autorizada consumiu os 37 comandos restantes; total da rodada 184/184. Comparação aproximada do mesmo alvo, com retorno observado à direção inicial e sessões novas: 42,806 segundos antes e 44,387 segundos com a candidata, nove comandos em ambos. Erros finais 6,305 e 3,710 pixels na análise de 960 pixels. Não houve ganho global demonstrado; rapidez e aceite integral continuam pendentes.

Fluxo óptico agrupado preservou exatamente máscaras e coordenadas em dezoito comparações reais, reduzindo essa etapa de 15–38 para 4–11 milissegundos. Correção fina apoiada em duas respostas consistentes foi exercitada fisicamente, mas não melhorou o tempo total. Seleção de eixo agora considera o erro angular restante previsto; a economia de um ciclo tem reprodução determinística, ainda sem comparação física equivalente. Nenhum critério de chegada ou parada foi relaxado.

Intenções podem reduzir explicitamente o limite de comandos; padrão e teto permanecem 16. Os dois últimos ensaios usaram sete: retorno chegou em 40,359 segundos; apontamento seguinte esgotou o limite em 38,884 segundos sem confirmar chegada. Parada registrada e vídeo localizado recuperado, com mensagem de apontamento incompleto. Esse resultado não aprova chegada nem representa uma tentativa completa de 16 comandos.

Verificações pertinentes: 52 testes de localização e 113 de navegação/sessão aprovados, Ruff e conferência de diferenças aprovados. Serviços próprios encerrados e ausência verificada nas portas 8100, 5174 e 9227. Sem nova aquisição panorâmica. Evidências privadas e diagnóstico final em `~/.codex/task-artifacts/toposync/panorama-ao-vivo/aceleracao/retomada-ampliada-20260924/janela-final-20260925/resultado.md`. Novos ensaios dependem de cota explícita e hipótese causal distinta; não repetir a mesma rodada para tentar obter um resultado melhor.

# Histórico — centralização demonstrada; rapidez ainda aberta, 24/09/2026

Retomada posterior à pausa, 24/09 à noite: corrigido o fallback do player compartilhado que mantinha `srcObject` da conexão WebRTC encerrada e impedia a reprodução seguinte. Timeout real passou a recuperar os dois vídeos. O serviço de mídia próprio precisou ser reiniciado para restabelecer WebRTC; a causa interna da interrupção UDP não foi determinada.

Recuperação visual agora combina propostas das duas fotometrias somente após ambas recusarem a localização, mantendo agrupamento de duplicatas/conflitos, partição independente e todos os critérios geométricos. Uma fotografia central recentemente reconhecida pode orientar a busca de outro decodificador, sem transferir pose, pixels ou identidade. A tentativa normal de fluxo óptico permanece primeira; uma escala adicional é tentada somente após insuficiência. Reconhecimento qualificado que expira na última tentativa pode desencadear uma única verificação com imagem nova; a pose vencida nunca autoriza movimento. As 65 imagens preservadas continuaram qualificadas e os invariantes de cache passaram.

Novas chegadas físicas: clique interno em seis comandos/36,469 s, erro 3,38 pixels; clique externo em dez comandos/47,171 s, erro 7,54 pixels. Os dois vídeos avançaram nos 35/35 e 46/46 intervalos amostrados. Um retarget expôs confirmação final de parada curta demais durante ajuste de exposição: a sessão passou a usar o orçamento de observação existente do scanner, preservando critérios visuais e expiração do destino. Retarget posterior, disparado após recibo de movimento aceito e antes da confirmação de Stop, concluiu com um comando antigo e três novos, erro 8,81 pixels e 62 pontos independentes de conferência. A época final lida sob a própria posse do controlador distingue a conclusão própria de um movimento externo e preservou o marcador de centro confirmado. Não há timestamps físicos certificados.

500 testes de scanner/captura/sessão passaram, além dos testes direcionados de localização e da época na interface; conjuntos históricos se sobrepõem. TypeScript e compilação da extensão passaram. Rapidez permanece sem aceite integral: ainda há reconhecimento dispendioso e confirmação entre pulsos. A gravação e os registros privados desta retomada estão no diretório externo `panorama-ao-vivo/aceleracao/retomada-ampliada-20260924/final-combined/`; o guia canônico preserva orçamento, hipóteses recusadas e os limites de cada evidência. Não há nova aquisição panorâmica.

A navegação reutiliza o controlador e o scanner existentes: adia sondagens do eixo secundário enquanto o eixo qualificado reduz o erro, e pode adaptar pulsos de aproximação até 1,2 segundo após movimento com suporte distribuído. A velocidade permanece 0,1. Ganhos são invalidados após mudança de enquadramento; pulsos ampliados exigem comparação visual prévia. Fotografia classificada para aquisição e quadro recente confirmado para navegação permanecem separados. Comparação geométrica precede a barreira final; replay NPZ continua sem perdas, com compressão nível 1 para reduzir trabalho após a confirmação.

A recuperação contextual compara referências em até quatro trabalhadores e tenta as duas formas fotométricas, original e contraste local, uma única vez cada após insuficiência. Descritores separados, raster original, recusa de ambiguidade, distribuição independente e frescor foram preservados. O detector de movimento também reaproveita a evidência distribuída contra a âncora inicial após perda transitória do rastreamento. Isso confirma somente transição; estabilidade ainda exige a janela global posterior ao Stop. Dois testes reproduziram a falha antes da correção; 79 testes do detector e 463 dos consumidores scanner/navegação/sessão passaram.

Dois novos percursos físicos noturnos chegaram: acabamento em cinco comandos/38,325 s, erro 4,49 pixels; alvo externo em 12 comandos/70,737 s, erro 1,97 pixels. Erros medidos na largura de análise 960, com pontos independentes; timestamps físicos não certificados. Retarget durante pulso observado, Parar, clique posterior e mudança externa foram exercitados nesta retomada. Marcador e indicação de centro confirmado são invalidados após movimento externo. Percursos distintos não permitem alegar aceleração causal; a rapidez continua sem aceite. Chuva atual não confirmada.

No último enquadramento, cinco minutos de MSE mantiveram a sobreposição, mas descartaram 7,64% dos quadros do principal. Reduzir a área gráfica e mudar apenas o perfil do encoder não melhorou; os dois experimentos foram revertidos. A panorâmica agora pede o contexto PTZ do player compartilhado, mantendo a fonte explícita. Na Frente, habilitada a saída WebRTC padrão pela política existente da publicação: 1280×720/15 fps. Sem transporte próprio ou alteração da fonte óptica. O plano mantém fallback e a geometria original da fonte; teste integrado confirma esse contrato.

Primeira janela WebRTC de cinco minutos: 300/300 amostras alinhadas, 360 observações qualificadas, nenhuma recusa; descarte principal 0,391%, teleobjetiva 0,300%, intervalos máximos 201,7/202 ms, sem lacuna acima de um segundo. Não é comparação com resolução constante: MSE era 1920×1080. Interrupção de oito segundos somente no publicador próprio retirou a projeção vencida, manteve a teleobjetiva e recuperou alinhamento cerca de três segundos após retomada, sem comando físico. Segunda janela de cinco minutos após reabertura: novamente 300/300 amostras alinhadas e 360 observações qualificadas, sem recusa; descarte principal 0,159%, teleobjetiva 0,340%, máximos de 168,1 ms, sem lacuna acima de um segundo. Aba oculta e janela minimizada por vinte segundos cada suspenderam players/análises e recuperaram alinhamento ao voltar, com visibilidade nativa conferida. Build e typecheck passaram; 54 testes de publicação/modelos e o contrato integrado ampliado passaram. O aceite integral permanece aberto.

Guia operacional e evidências privadas: `/Users/c/Downloads/codex-goals/toposync-panorama-ao-vivo-goal.md`, seção 28.9, e `/Users/c/.codex/task-artifacts/toposync/panorama-ao-vivo/aceleracao/retomada-ampliada-20260924/`. Saldo físico de nove comandos não permite reservar novo percurso de 16; não renovar orçamento implicitamente. Nenhuma nova aquisição panorâmica, alteração de firmware/credenciais ou comando Z. Alterações locais em development, sem publicação. Recursos próprios encerrados e portas 8100/5174/9227 livres. Readback final nativo IDLE sem valores PTZ; controlador idle/settling, geometry_safe=false, exigindo nova qualificação na retomada.

## Rodada anterior — aceite parcial

O reconhecimento nativo alimenta o rastreamento compartilhado com correspondências das fotografias originais, mantendo conferência geométrica e recusa de ambiguidade. A navegação reaproveita decisões ainda válidas sem abrir antecipadamente outra janela: a barreira de um segundo após análise e antes de cada pulso permanece. A alça das prévias foi separada da camada do player, o botão Parar ficou junto aos controles da visualização e falhas de conexão recebem orientação em português.

Na Frente, a janela corrigida gravada teve 791 localizações qualificadas e uma observação superada por uma intenção nova. O vídeo avançou nas 178 comparações entre amostras durante movimento. Um alvo curto foi confirmado com um pulso, reduzindo o erro de 30,14 para 10,28 pixels na análise de largura 960, dentro do limite existente de 12. Troca de destino, botão Parar, movimento externo, redimensionamento, navegação sem PTZ e recuperação foram exercitados. No Chrome sem emulação de foco, troca de aba e minimização reais suspenderam players e análises por 20 segundos cada, com retorno alinhado.

**Aceite integral pendente:** o alvo fora do campo atual avançou corretamente, mas não chegou antes do limite de seu ensaio; rapidez e chegada longa continuam abertas. O último teste curto foi interrompido na segunda correção e não conta como chegada. Saldo desta rodada esgotado: 23/23 comandos, 40/40 adicionais acumulados, Frente 121 cumulativos conservadores, Garagem zero. Nenhuma ampliação implícita. Guia externo específico, seção 28.8, e evidências privadas em `~/.codex/task-artifacts/toposync/panorama-ao-vivo/aceleracao/ensaio-20260924T182541Z/resultado.md`. Alterações locais, sem publicação nesta rodada.

## Preparação anterior — diagnóstico reutilizável

O localizador compartilhado conserva a decisão e suas evidências por quadro. Consultar o diagnóstico não repete reconhecimento, não renova a âncora de rastreamento e não altera o frescor. Fonte, sequência, bytes, dimensões e transformação continuam vinculando o resultado; o cache permanece limitado a quatro entradas por modelo. O retorno público não inclui as correspondências privadas.

A regressão foi reproduzida antes da correção. A conferência offline preservou as orientações das 65 imagens do conjunto anterior, sem reconhecimento repetido nas consultas ao mesmo quadro. Inicialização, continuidade da projeção e tempo de chegada ainda exigem comparação ao vivo; nenhuma velocidade, limiar geométrico ou proteção física foi alterada. Retomada e evidências no guia externo específico, seção 28.7. O aceite físico integral continua pendente.

## Histórico — retomada noturna em validação, 19/09/2026

Continuação autorizada do plano N1–N5 do guia externo específico. Prazo único: 19/09 23:51:38 UTC a 20/09 03:21:38 UTC. Quarenta comandos adicionais autorizados, **dezessete usados**; histórico anterior Frente 81, Garagem zero. Sem aquisição, commit, push ou release.

- N1–N3: 47 inputs noturnos separados em 24 de diagnóstico e 23 reservados. Reconhecimento contextual e acompanhamento de detalhes integram o localizador compartilhado, sem alterar referências, geometria, frescor ou proteções. v5: 176 testes direcionados, Ruff e replay independente de 65 imagens aprovados; acompanhamento após bootstrap com mediana 238 ms e máximo 338 ms. Esses resultados são offline.
- N4: duas janelas reais de cinco minutos com v4 tiveram 600/600 amostras alinhadas, mas início/reabertura e posição posterior ao movimento ainda oscilaram. Os vídeos continuam fluindo durante a recusa da projeção. v5 reduz trabalho inútil após perda transitória, mas sua nova abertura também reprovou continuidade. Nenhum aceite geral de estabilidade.
- Diagnóstico atual: todas as vinte referências recusam um quadro da posição final. Um candidato tem 19/20 holdouts corretos e p95 de 4,97 pixels, mas cobertura independente de 10,6%, abaixo de 20%. Manchas/reflexos amplos visíveis e correspondências concentradas; não basta acelerar nem trocar a referência. Gates preservados.
- N5 parcial: clique interno finalizou naturalmente com centro observado a 7,78 pixels, 24 holdouts e p95 de 7,43; oito comandos nos dois segmentos, parada física observada. Timestamp físico da captura não verificado; usada observação local recente. Descoberta e corrigida sequência desatualizada na interface após Stop externo: teste reproduziu antes, passou depois e primeiro clique real seguinte executou. Suite de interface: 14 aprovados, um replay privado opcional ignorado; typecheck/build aprovados.
- Redimensionar/mover/minimizar prévias e navegar sem comandos foram exercitados. Falha de observação e reconexão mantiveram vídeo bruto e recuperaram sessão. Ocultação real foi comprovada após remover emulação de foco do instrumento: cerca de 67 segundos sem análises ou consumidores; retorno com sessão nova. Substituição real chegou a 8,71 pixels, com um comando antigo interrompido e quatro no último destino. Clique externo executou três comandos, mas não chegou. Parar específico ficou inconclusivo: ocorreu encerramento de sessão, que confirmou parada física de um comando. Movimento externo coordenado não executado. A captura final do movimento interno falhou; screenshot e replays existem, vídeos anteriores são amostrados e têm lacunas declaradas.

Evidências privadas em `.toposync-data/live-panorama-validation/noite-chuva-20260919T235138Z/`: `N5-internal-final-result.json`, `N5-internal-center-confirmed.png`, `conferencia-visual.html`, `v5-spatial-support.png`, contratos, auditorias, ledgers e vídeos. Linhagem e limitações nas seções 27.5–27.16 do guia externo. Trabalho paralelo preservado; núcleo ainda sem aceite integral. Resultado final desta janela em `noite-chuva-20260919T235138Z/resultado.md`; saldo numérico não renova o prazo.

## Histórico — correção fotométrica, aceite parcial, 19/09/2026

Continuação mínima autorizada: o localizador compartilhado conserva a tentativa nativa e usa contraste local normalizado somente após insuficiência de suporte. Reutiliza OpenCV, referências e critérios geométricos existentes; não substitui resultado nativo qualificado ou ambíguo. Nenhuma mudança em viewport, player, controlador ou geometria nesta etapa.

- Seis frames reais antes recusados foram recuperados e doze positivos preservados. Regressão de contraste controlado reproduziu a falha e passa após a correção; 86 testes dirigidos e Ruff aprovados. A variante de normalizar sempre perdeu um positivo e foi rejeitada.
- Inspeção visual da fotografia recuperada no renderer existente confirmou o encaixe após corrigir somente o manifesto do teste para usar a conversão canônica dos eixos. Captura e vídeo identificados como replay; não certificam transmissão nem movimento real.
- Duas janelas reais de cinco minutos, antes/depois de reabrir: principal com 12,830/13,766 quadros/s apresentados, 0,594%/0,386% de descarte, maiores intervalos 641,6/321,5 ms. Nenhuma pausa, espera, reset ou erro. Todas as observações ficaram sem alinhamento confirmado. Continuidade da prévia aprovada nessas janelas; sobreposição positiva não aprovada.
- Cena noturna atual: no máximo 21 correspondências recíprocas entre todas as vinte referências, concentradas em portão/muro. Falta suporte independente distribuído para o mínimo existente de sessenta grupos. Três frames ao anoitecer e quatro noturnos continuam recusados. Sem pose antiga, redução de proteção ou aquisição nova.
- Corrupção HEVC histórica não reproduzida: 745 pacotes comuns idênticos entre origem configurada e relay; 734 hashes decodificados idênticos com uma thread e paralelismo automático, sem avisos. Quadros inspecionados íntegros. Não declarada corrigida.
- Redimensionar, mover/minimizar/restaurar teleobjetiva e navegar no panorama não emitiram comandos. A repetição de ocultação foi inconclusiva: a página continuou reportando `visible` apesar da troca de aba na interface nativa. Não substitui a prova anterior de suspensão.

Cota adicional autorizada nesta continuação: **40 comandos; zero usados**. Histórico Frente 81, Garagem zero. Janela máxima 21:36–23:36 UTC. Chegada externa, chegada após substituição e Parar seguem pendentes por falta de localização atual qualificada. Evidência privada: `.toposync-data/live-panorama-validation/ponytail-20260919/resultado.md`; guia externo, seção 26. Trabalho paralelo e guia da aquisição preservados; sem commit, push ou release.

## Histórico — cadência corrigida e investigação anterior

Investigação posterior sem novos movimentos físicos. O agendador compartilhado `StreamWriterBridge._run_loop` acordava a cada 100 ms, limitando saídas configuradas para 15 quadros/s a no máximo dez. Medição real anterior: publicadores principal/teleobjetiva em 9,07/9,34 quadros/s. Regressão determinística reproduziu o teto; o agendador agora considera o próximo prazo das saídas ativas, preservando a espera ociosa, a política de falha e a ausência de rajadas de recuperação.

- Nova janela real de 300,089 segundos: principal apresentou 13,443 quadros/s, descarte 2,008%, maior intervalo 399,8 ms; teleobjetiva 13,999 quadros/s, descarte 0,024%, maior intervalo 240,1 ms. Sem pausa, reset ou erro terminal. Publicadores medidos em 13,957/14,344 quadros/s; fonte, resolução e codec preservados. Fluidez aprovada nessa janela, sem transformar o limite configurado de 15 em taxa observada.
- Registro: 563/601 amostras alinhadas, 37 sem alinhamento confirmado e uma localizando. Não aprovado para continuidade total. Três quadros recusados preservados **depois** da janela também falham offline, inclusive contra todas as referências: dois candidatos chegam a somente 56/57 grupos independentes, abaixo dos 60 exigidos; os demais não alcançam o mínimo de correspondências. Reflexos/iluminação mudaram visivelmente, mas sua contribuição causal não foi isolada. Critérios e validade da pose intactos.
- Corrupção HEVC anterior não reproduzida: os 296 pacotes da origem configurada e da ingestão local foram idênticos numa gravação privada simultânea de 12 segundos. Decodificações com paralelismo automático e uma thread produziram hashes idênticos, sem erro. Isso não absolve permanentemente nenhum componente; faltam os bytes comprimidos do instante corrompido. O indicador `healthy` limpa erros quando há frame recente e não comprova integridade dos pixels; reprodução mínima preservada.
- 24 testes de agendamento/publicação/resize e cinco de publicação integrada passaram; Ruff e verificação do diff aprovados. O servidor local foi recarregado somente após prova de zero espectadores, publicadores e demanda, além da posse do processo desta tarefa. A recusa inicial da revisão automática foi resolvida com essa evidência. Instrumentação temporária removida.

Evidência privada e conclusões: `.toposync-data/live-panorama-validation/investigacao-captura-20260919/resultado.md`. Delta desta investigação: agendador e seus testes; sem mudanças geométricas. Ledger físico permanece **40/40 nesta etapa, histórico Frente 81, Garagem zero**. Sem aceite geral, nova varredura, push ou release.

## Histórico — correções e ensaio físico anteriores nesta retomada

Retomada autorizada do plano da seção 22 do guia específico. Geometria, mídia, aproximação final e recuperação corrigidas no checkout real, preservando o viewport da calibração, players, controlador, imagens e geometrias existentes. O guia da aquisição e suas cotas não foram usados nem alterados. Nenhuma nova varredura.

- Localização: agrupamento de correspondências físicas, partição estável independente, filtro compartilhado de sobreposição fixa e RootSIFT; frescor, distribuição e erro de conferência mantidos. Replays privados conservam os últimos inputs exatos após Stop, com limite de armazenamento.
- Mídia: timestamps reais de chegada dos frames brutos corrigem a cadência comprimida; o player real mantém conexões saudáveis durante renovação de credenciais e recupera falhas terminais. Janela inicial de 309 segundos: 619/619 amostras alinhadas, 0,72% de descarte principal, sem pausa/reset/erro terminal.
- Centro real interno: duas sondagens qualificadas, erro final **7,133 pixels** em análise de 960, percentil 95 independente 3,088 pixels. Imagem bruta e resultado confirmados; não foi promovido apenas pelo marcador na tela.
- Exterior e retarget: corrigida a visita desnecessária a referências intermediárias quando o raio final já está visível. Primeiro alvo externo terminou no teto de 16 comandos. No ensaio seguinte, o primeiro destino foi substituído após seis comandos e parada observada; somente o segundo continuou. A guarda encerrou a sessão no décimo comando, última medição 19,50 pixels. **Sem chegada aprovada** para esses casos.
- Stop: a guarda solicitou parada, mas a primeira observação ficou sem prova por quadros repetidos e lacuna de captura. Sessão bloqueada, sem relaxar proteção. A revisão automática impediu reconectar antes de nova evidência. Posteriormente 47 quadros em seis segundos, 15 imagens distintas, deslocamento máximo 0,064 pixel e controlador estável permitiram reabertura. O botão Parar não recebeu aceite físico nesta rodada.
- Movimento externo: um pulso coordenado pelo controle manual existente mudou a cena; a projeção se atualizou e o controlador retornou estável. A interface também observa a época do controlador para detectar pulsos que terminam entre consultas. Reconexão limpa marcador, intenção e respostas da sessão anterior.
- Navegação e painéis: zoom/arraste, redimensionamento e minimização exercitados sem intenções PTZ. Chrome real oculto por 55 segundos: dois players suspensos, zero análises novas após o trabalho em voo, zero comandos. O navegador embutido de automação não representa corretamente esse ciclo de visibilidade.
- Janela posterior após movimento/reabertura, sob carga de CPU concorrente: 328,5 segundos, 641/658 amostras alinhadas e descarte principal de 9,31%. **Não aprovada** para estabilidade/fluidez. Não substituir esse resultado pelo primeiro enquadramento favorável.
- Chrome real após recuperação, sem a carga paralela anterior: 370,503 segundos, 742/742 amostras alinhadas e 397 localizações aceitas; zero comandos, pausas, resets ou erro terminal. Descarte principal de 5,174%, ainda acima do objetivo de 5%; teleobjetiva sem descarte. Maior intervalo de apresentação 519,6 ms. Registro aprovado somente nessa janela, fluidez geral ainda sem aceite.
- Falha de conexão provocada somente no consumidor desta tela: recuperação de vídeo e alinhamento em 3,235 segundos, sem comandos. Reabertura final com a instrumentação temporária removida manteve os dois vídeos ativos e descartou o marcador anterior.
- Qualidade da imagem ainda reprovada: blocos coloridos já aparecem no frame 3840 × 2160 do hub, antes de publicador, navegador e projeção. O decodificador registra erro HEVC, embora o indicador agregado diga `healthy`. O trecho localizado é captura/ingestão/decodificação; ainda não se determinou qual desses componentes origina a corrupção. Fotografias e métricas preservadas, sem reduzir resolução nem alterar a configuração para ocultar o defeito.

Ledger da retomada: **40/40 comandos contabilizados**, histórico Frente 81, Garagem zero. O comando final interrompido/pending foi contado conservadoramente. Não emitir outros movimentos nesta etapa. Permanecem sem aceite completo: estabilidade sob carga, fluidez e qualidade da imagem, chegada de alvo externo, chegada após retarget e botão Parar durante movimento. Testes simulados não fecham essas lacunas. Leitura final às 20:45:13 UTC: `IDLE`, época 35, sem operação; qualificação geométrica `settling/geometry_safe=false`. A evidência anterior de parada não deve ser promovida a autorização atual para outro movimento.

Verificações direcionadas aprovadas: 69 testes de navegação/sessão após as correções mecânicas; 14 do localizador, com controles de deduplicação, partição e replay; 17 de publicação/cadência/recuperação; 13 cenários de interface e dois do player com H.264/MediaSource reais. Um cenário opcional com fotografia privada foi omitido. Tipos e builds aprovados, com avisos existentes de tamanho de bundles. As suítes se sobrepõem a rodadas anteriores; não somar contagens.

Evidências privadas: `.toposync-data/live-panorama-validation/correcao-20260919/`; índice `resultado.md`, ledger `physical-manifest.json`, manifesto final `final-source-manifest.json`, resultados e replay em `physical/`, janelas passivas e resumo de suspensão do Chrome. `fluxo-centralizacao-capturas.mp4` contém **14 capturas amostradas ao longo de 20,363 segundos**, com os intervalos preservados e dois segundos de retenção final; não é filmagem integral do movimento. Alterações locais revisáveis, sem push ou release.

## Histórico — investigação e plano anteriores desta mesma meta


Plano de correção e lista operacional preparados na seção 22 do guia externo específico: dez tarefas, dependências e marcos de geometria, relocalização, player real, interação e aceite físico. Esta etapa foi apenas de planejamento. `tasks/plan.md`, `tasks/todo.md` e o guia da aquisição permanecem preservados; orçamento e aceite não foram renovados pelo plano.

Investigação posterior reproduziu a alternância com os bytes originais, sem navegador ou câmera. A partição de ajuste/conferência por índice (`% 5`) muda quais correspondências falsas recebem rejeição robusta: o mesmo relógio embutido e o mesmo ponto ruim do primeiro plano estão na conferência do frame recusado e no ajuste do aceito. Reordenar o mesmo conjunto de pontos inverte o resultado. Também há dois descritores do mesmo ponto divididos entre ajuste e conferência. A reconstrução já exclui sobreposições fixas, mas o localizador não reutiliza esse tratamento. Só retirar o relógio não resolve: o percentil 95 ainda fica em 19,84 pixels. Diagnóstico e reprodução mínima privados em `.toposync-data/live-panorama-validation/pratico-frente-20260919/diagnostico/diagnostico.md`; guia externo, seção 21. Nenhuma correção de produto nem novo movimento nesta investigação. A causa exata dos descartes MSE e dos frames recusados durante os pulsos continua aberta; fotografias posteriores não substituem esses frames.

Uma nova observação real da Frente, entre 17:10:58 e 17:14:39 UTC, contradisse o aceite geral de estabilidade anterior. Em 221 segundos, com a aba visível em todas as 885 amostras e zero comandos físicos, ocorreram 67 mudanças de estado. Houve 170 recusas de localização e 59 registros aceitos nas respostas instrumentadas. A projeção encaixa visualmente quando aceita, mas desaparece repetidamente e volta à prévia bruta. O player principal também apresentou espera frequente, descarte de frames e uma reinicialização de mídia. O tempo interno de reprodução não deve ser interpretado como atraso de captura: os relógios nas imagens preservadas estavam próximos do horário da observação.

Evidências privadas novas: `.toposync-data/live-panorama-validation/pratico-frente-20260919/observacao-passiva-completa.json`, `resumo-passivo.json`, `frame-unlocalized.jpg`, `frame-localized.jpg`, capturas em `gravacao/` e `oscilacao-real.mp4` (sequência de dez capturas, espaçadas aproximadamente três segundos). Os frames recusado e aceito, com relógios 14:13:29 e 14:13:30, mostram o mesmo enquadramento. A perda repetida de registro está confirmada; a causa exata da recusa geométrica e da cadência MSE ainda precisa de diagnóstico localizado. Não ampliar validade da pose nem reduzir critérios para ocultar o problema.

Após autorização explícita para até 32 comandos adicionais, no máximo 16 por destino, os dois cliques reais foram executados na interface. O alvo interno (base do poste à direita) e o externo (trecho da via à esquerda) emitiram três comandos cada. Ambos terminaram com `panorama_visual_localization_failed`, sem centralização certificada. A câmera respondeu e as seis paradas foram observadas; o giro para a esquerda no segundo caso foi confirmado nas imagens, mas não alcançou o centro solicitado. Um clique preliminar à esquerda foi interceptado pela prévia flutuante que apareceu sobre o ponto e não gerou intenção física.

Nas janelas de 22,637 s e 24,663 s, o vídeo principal nunca pausou, não reiniciou e manteve `readyState >= 2`; ainda houve 140/219 e 173/239 frames reportados como descartados. Continuidade da prévia foi observada; fluidez e centralização permanecem reprovadas. Os seis movimentos tiveram transição visual, estabilização e confirmação do quadro final. A falha está localizada na relocalização após o movimento, não na entrega do clique ao controlador. A razão geométrica específica ainda exige diagnóstico com imagens preservadas, sem afrouxar critérios.

Ledger atualizado: base Frente 35, acréscimo 6, total 41; teto autorizado desta rodada 32 adicionais, 6 usados. Rodada encerrada após os dois destinos, sem novos movimentos por repetição. A câmera reportou `IDLE` às 17:31:49 UTC; o controlador ainda indicava `motion_state=settling` e `geometry_safe=false`, sem promovê-lo a qualificação geométrica. Evidências e relatório: `.toposync-data/live-panorama-validation/pratico-frente-20260919/cliques/resultado.md`, resultados físicos, instrumentação, fotografias e `clique-dentro-capturas.mp4` (10,56 s, capturas amostradas do trecho final, não gravação integral). O serviço paralelo de poses confirmou uso exclusivo de replay, sem câmera física/PTZ compartilhado. Nenhum código, panorama, recorte, credencial ou firmware foi alterado neste novo ensaio. Guia externo atualizado na seção 20; aceite atual permanece aberto.

## Evidência histórica anterior — alcance limitado aos ensaios descritos

Implementação e contratos em [Panorâmica ao vivo](panoramica-ao-vivo.md). O modo reutiliza `NavigableViewport`, o player e os transportes do host, o modelo esférico Brown/racional, o localizador de referências e o controlador PTZ existentes. O seletor superior escolhe a câmera panorâmica; o recorte salvo é automático; as prévias principal e de teleobjetiva são redimensionáveis, e a segunda só aparece quando há fonte real. Gestos do viewport e do PiP não comandam PT nem zoom óptico.

- A causa residual da piscada foi isolada: uma resposta visual `unlocalized` apagava imediatamente uma pose recente mesmo quando o comparador global confirmava a mesma vista. A pose agora sobrevive apenas dentro da própria janela curta de frescor, com mesma época e imagem global compatível; a comparação não renova a pose. Movimento, identidade nova, expiração e erro terminal continuam invalidando.
- Observação final: 137,6 s, 550/550 amostras em `Vídeo alinhado`, zero alternâncias, pausas, `stalled`, `emptied`, erros ou comandos. Houve 890 `waiting` e 889 `playing` no MSE, confirmando que a saúde do transporte não foi ocultada.
- Centralização física: sessão `ba1aa3ba89064afd8a9c428aa0a24829`, sequência 4, três comandos, `visual_aim_verified`, erro final 1,58 pixel em 960 pixels e percentil 95 de validação 3,69 pixels.
- Retarget físico: sessão `5596d5f449744d45b1bc317928ac74e8`. A sequência 1 já estava em `moving`, recebeu a sequência 2 como pendente e terminou `superseded` com zero comandos e sem resultado. Somente a sequência 2 executou três comandos; confirmou centro com erro 10,04 pixels, 26 correspondências e percentil 95 de 4,20 pixels. A prévia bruta avançou por 13,6 s, sem pausa nem queda abaixo de `readyState=2`; o resultado antigo não apareceu.
- Ledger cumulativo: Frente 35/40 comandos; Garagem 0/40; um piloto usado. Nenhuma nova aquisição ou varredura, nenhum firmware, credencial, recorte ou panorama alterado. O enquadramento final foi preservado.
- Validação final: 473 testes Python, seis testes Node, typechecks da extensão, frontend e API de plugins, builds do frontend e da extensão e oito cenários Playwright aprovados; um replay opcional com fotografia privada permaneceu ignorado. O navegador cobriu gestos sem comandos, espera breve e falha terminal, retarget, última intenção, Stop, movimento externo, ocultação/retorno, reconexão e troca de referência.

Evidências privadas: `.toposync-data/live-panorama-validation/validacao-20260919/aceite-fisico.json`, `estabilidade-final-137s.json`, `real-frente-preview-and-telephoto-final.png` e gravações em `.toposync-data/live-panorama-validation/ui-results/`. O movimento causado pelo clique teve aceite físico; a reação a movimento iniciado externamente foi validada de forma integrada, sem gastar outro comando físico dedicado. Plano, execução e decisões estão no guia externo `/Users/c/Downloads/codex-goals/toposync-panorama-ao-vivo-goal.md`. Os estados e limites da aquisição abaixo permanecem históricos e separados.

---

# Checkpoint atual — calibração assistida, 18/09/2026

- Simulação adicional solicitada: oito pontos preenchidos pela interface, conclusão/ativação e três locais independentes projetados nos dois sentidos. Cenário sintético aprovado; erro máximo 1,423 mm no chão idealizado e 0,0109 pixel no sentido inverso. Mais 53 testes de processamento/localização aprovados. Não certifica a Frente real. Relatório: `tasks/calibracao-assistida/simulacao-completa.md`.

- Uma entrada “Calibrar câmera” no editor. A panorâmica compatível abre diretamente o assistente; rascunhos retomam as marcações. Sem imagem compatível, a preparação permanece no assistente e continua após validar a imagem publicada; captura exige ação explícita.
- Seis pontos de ajuste válidos, dois de conferência independente e “Concluir calibração”. Revisão, pontos extras, desfazer e diagnósticos ficam recolhidos. Resultado concluído compacto; apontamento físico é uma ação separada.
- Retomada idempotente valida composição, elemento, fonte, artefato e revisão. “Revisar calibração” cria rascunho separado, sem editar o ativo ou reutilizar o registro anterior protegido. Ativação e restauração mantêm os controles de conflito. Vistas antigas e trabalho mecânico paralelo preservados.
- Validação: 42 testes da API, 24 testes Node e 16 cenários Playwright aprovados; typechecks da extensão, frontend e plugin API aprovados; bundle da extensão compilado (aviso de tamanho já existente). Testes usam câmera sintética, não certificam cobertura nem apontamento físico.
- Chrome real: Frente Reolink abriu diretamente a panorâmica 4096 × 2048 e recuperou o par pendente do navegador. Nenhum ponto foi confirmado, nenhuma calibração ativada, nenhuma captura ou movimentação solicitada. Comparação do arquivo de configuração antes/depois: idêntico.
- Instância local reiniciada com autenticação normal, após verificar ausência de capturas em andamento. Evidências privadas: `ignore/calibracao-assistida-20260918/`. Plano, tarefas e validação: `tasks/calibracao-assistida/`.

---

# Histórico — navegação reutilizável, 18/09/2026

- `host.ui.NavigableViewport` disponível para conteúdo DOM, imagens, SVG e canvas. A planta compartilha os cálculos de rotação, arraste e zoom; suas ferramentas especializadas foram preservadas.
- Integrado na prévia de configurações, imagem de mapeamento e editor da área útil. Roda ancorada no cursor, arraste, pinça, teclado e ajuste à área. Navegar preserva as coordenadas; marcadores mantêm alvos de 44 pixels.
- Redimensionar após navegar conserva centro e escala absoluta. Botões, pontos e recorte continuam editáveis; dois dedos cancelam somente o gesto de edição em andamento. Previsões temporárias desaparecem ao iniciar navegação.
- 24 testes Node e oito cenários Playwright focados aprovados, incluindo mapeamento completo sintético, teclado, previsão bidirecional, cancelamento de recorte por pinça e payload canônico do recorte. Typechecks e builds aprovados; avisos de tamanho de bundles permanecem.
- Chrome real confirmou zoom pela roda, arraste e ajuste na Frente; editor abriu e fechou sem salvar. Compilação final recarregada. Sem novas capturas, comandos físicos, mudanças no recorte salvo ou no mapeamento real. Backend mecânico e trabalho paralelo preservados.
- Plano e evidências: `tasks/navigable-viewport/`. Contrato de reutilização: `docs/ui/navigable-viewport.md`. Esta entrega de interface não certifica cobertura nem retorno físico da câmera.

---

# Histórico — publicação integrada, 18/09/2026

- Reinício e tomada da instância expressamente autorizados pelo usuário nesta tarefa. Backend anterior 92014 encerrado; backend 29093 serve a porta 8100 a partir do checkout, com autenticação normal. Evidências e backup privado em `ignore/panorama-publication-restart-20260918/`.
- Comparação com `runtime-r07`: 37 arquivos Python idênticos; única diferença de backend é a publicação automática em `source_panorama.py`. Trabalho mecânico paralelo preservado. Nenhuma nova aquisição, montagem ou comando de movimento emitido nesta etapa.
- Frente: abertura autenticada promoveu `6c3a151fc83a4ccaacaac2ba4fa292b2` a ativo, preservou `0e436fe302b24d73a0c36a2d8737c370` como anterior e removeu a candidata. Revisão do ponteiro 77. Chrome confirmou a imagem nova 4096 × 2048 no editor e o botão “Usar panorâmica salva” habilitado; calibração existente preservada.
- Interface concluída compactada também quando existe retorno pendente: cobertura parcial e retorno não confirmado continuam explícitos; resultados detalhados e remontagem ficam em “Detalhes”. Isso não aprova cobertura nem apontamento.
- Validação: 274 testes de backend, três cenários Playwright (incluindo parcial com retorno pendente e promoção legada), três testes de traduções, typecheck e build aprovados. Confirmação adicional no Chrome real e configuração persistida em `verification.json`.

---

# Histórico — Corredor, continuação encerrada em 18/09/2026

- Uma aquisição autorizada e executada (1/1), dentro da janela 18:20:38–18:50:38 UTC. Tracking e patrulha desligados por confirmação humana; nenhuma configuração alterada pelo agente. Nenhuma operação nas outras câmeras.
- Revisão congelada 07, política 4; 38 hashes conferidos antes da captura. Código e trabalho paralelo preservados, sem nova correção ou repetição de testes válidos.
- Trabalho `9e309c08674c4ddb9395d048389d73a2`: referência e apoio inferior, duas fotografias, nenhuma faixa completa. Primeiro bloqueio: confirmação final do segundo tilt recusada por `fresh_frame_unavailable` / `endpoint_identity_unverified`, com troca de transporte e identidade de captura. Houve movimento observado; não é prova de ausência de efeito mecânico.
- Integral parcial `4e18a18380914c81b10c0bee5657ec26`, 4096×2048, máscara, originais e geometria preservados; reaberto pelo endereço do próprio Toposync. Artefato ativo anterior preservado; seleção do candidato no painel principal não demonstrada.
- Aquisição incompleta; reconstrução em revisão; referência preservada e estreito acréscimo inferior, sem expansão bilateral. Parada observada no encerramento; retorno pendente; controlador posteriormente UNKNOWN / geometry_safe=false. Sem aceite de cobertura ou prontidão.
- Resultado: `/Users/c/Downloads/codex-goals/toposync-panoramica-evidencias/20260918-corredor/resultado-corredor.md`. Ledger e validação no mesmo diretório. Rodada encerrada sem segunda aquisição ou nova investigação.

---

# Histórico — emenda de 18/09/2026

Guia canônico: `/Users/c/Downloads/codex-goals/toposync-panoramica-goal.md`.
Resultado e evidências: `/Users/c/Downloads/codex-goals/toposync-panoramica-evidencias/20260918-emenda/resultado-emenda.md`.

- Rodada adicional: 14:54:55–16:54:55 UTC, sem renovar limites por retomada. Ledger separado; Térreo 2/2, demais 0/1.
- Revisão07: qualificação existente de ausência de efeito precede análise tardia dispendiosa; teste de regressão e verificações direcionadas. Térreo exerceu a localização06, mas não adquiriu cobertura inferior/bilateral. Cinco originais preservados, integral parcial `b5a057bfb8b14587ab9221f387558f58`, reconstrução em revisão, parada observada e retorno pendente.
- Corredor depende da confirmação dos modos no aplicativo; Quintal recusou conexão TCP ONVIF. Garagem e Frente anteriores preservadas, sem nova captura. Não há aceite global.
- Trabalho paralelo de interface/publicação do usuário preservado. Backend local usa snapshot externo07 explícito; não transportar aprovação para mudanças do checkout que não foram executadas.

---

Histórico preservado abaixo:

# Checkpoint — pronto para aceitação física

15/09/2026. **Nenhuma câmera acessada ou movimentada nesta etapa. Nenhum processo do produto reiniciado. Nenhuma cobertura aprovada fisicamente.**

- Emenda autorizada registrada no contrato (§5 e §9): não procurar novamente o quadro histórico ausente. Causa visual da recusa histórica da Garagem desconhecida; reprodução negativa histórica não aprovada.
- Revisão base: `0ca8d6bc0b61ad1e746a9c59b7476c6cda087cf3`, com alterações locais preexistentes preservadas. Nenhum commit, staging ou reversão. Inventários antes/depois e hashes em `ignore/coverage-implementation-20260915/`.
- Implementação: `panorama_coverage.py` (novo), `panorama_region.py`, `panorama_scan.py`, `source_panorama.py`, `plugin.py`; interface em `types.ts`, `CameraSourcePanoramaSection.tsx`, `sourcePanoramaTranslations.ts`; testes em `tests/test_camera_panorama_coverage.py`. Documentos: contrato emendado, decisão, este checkpoint e `protocolo-aceitacao-fisica.md`.
- Política 4: referência integral → inferior → semente lateral → lado oposto → extensão lateral restante. Recusa local só permite outra região após localização qualificada. Ledger cumulativo v3, 24 fotos/30 comandos/420 s, reserva 5/150; sem retomada automática da rota interrompida. Históricos v1–v3 preservados.
- Primeiro par recusado conservado sem perdas (NPY), identidades/métricas/comandos, até 64 MiB de imagens e 1 MiB de metadados; indisponibilidade explícita ao exceder. Matcher, critérios geométricos, executores e reconstrutor preservados.
- Validação final: **70 testes direcionados passaram**, 702 fora do escopo, em `tests-final.txt`; TypeScript da extensão passou, `typecheck-result.json`. Inclui 28 casos da nova cobertura, publicação/recorte, incerteza, orçamento real de `_move`, localização, versões anteriores e guards de geração/frescura. Entradas de trajetória são simuladas. O teste positivo com originais da Garagem foi executado, não pulado. `unchanged-components.json` compara os símbolos reutilizados com o baseline anterior às edições.
- Montagem pronta e percurso concluído não aprovam cobertura. Integral, máscara, metadados e geometria seguem o formato existente; ativo anterior permanece e o novo fica candidato. A primeira publicação pode ser parcial explicitamente.
- Próximo passo único: [protocolo de aceitação física](protocolo-aceitacao-fisica.md), uma Garagem e uma Frente, somente após qualificação operacional. Pendências: cobertura nova bilateral/inferior real, preservação das regiões úteis, localização entre visitas, orçamento físico, coerência visual, publicação/reabertura e estados de parada/retorno/controlador. Não iniciar esse protocolo nesta etapa.
# Continuação noturna de 19/09/2026 — localização e sobreposição

Guia desta execução: `/Users/c/Downloads/codex-goals/toposync-panorama-ao-vivo-goal.md`, seções 27.5–27.9. Janela única de 23:51:38 UTC a 03:21:38 UTC; quarenta deslocamentos adicionais, ainda zero consumidos neste checkpoint. Esta continuação não modifica o contrato nem o guia da aquisição.

- N1 preservou 47 inputs reais da Frente à noite, com piso molhado e flare variável: 24 de diagnóstico e 23 reservados. Fotografias panorâmicas e modelo permanecem imutáveis.
- N2 isolou a falta de correspondências confiáveis entre dia/noite. Maior resolução e AKAZE reprovaram os gates originais; DISK/LightGlue recuperou suporte contextual usando ONNX Runtime já existente, sem nova dependência de pacote ou envio de imagens.
- N3 aprovado offline: 175 testes direcionados e 65/65 imagens do replay v3 qualificadas, incluindo 18 positivos diurnos. Acompanhamento noturno após identificação inicial: mediana 355 ms, máximo 697 ms. Contratos anteriores rejeitados permanecem preservados; nenhum limiar de erro, holdout, cobertura, ambiguidade ou frescor foi relaxado.
- N4 em observação real: primeira sobreposição noturna renderizada no navegador, com referência em cinza, vídeo em cores e teleobjetiva real. Ainda não equivale ao aceite das duas janelas prolongadas nem ao aceite físico N5.

Ligação compartilhada em `extensions/cameras/src/toposync_ext_cameras/processing/panorama_localization.py` e `panorama_features.py`. O fluxo óptico bidirecional existente foi extraído de `panorama_stability.py` para `panorama_correspondences.py`; cada quadro reajusta a orientação contra pixels das fotografias originais, mantendo âncora fixa e conferência independente. Âncoras válidas são medidas antes do reconhecimento custoso; perda de suporte exige relocalização. O seletor existente `target_reference` acrescenta no máximo uma fotografia original mais central, mantendo as alternativas na verificação de ambiguidade.

Artefatos privados: `.toposync-data/live-panorama-validation/noite-chuva-20260919T235138Z/`, em particular `benchmark-contract-v3.json`, `benchmark-results-v3/validation.json`, `reload-proof.json` e `live-aligned-start.png`. Job local-worker `6526e1fbc92b4ce3b5dac79ae0b43745` concluído com validador aprovado e auditoria independente. Nenhum teste offline certifica deslocamento físico.


Observação final desta retomada (24/09/2026): 300/300 amostras alinhadas durante cinco minutos, sem recusa de localização e sem intervalo de apresentação superior a um segundo nos dois vídeos. Janela de execução encerrada pelo prazo original, serviços próprios desligados. Rapidez permanece sem aceite; a próxima validação física depende de nova janela autorizada, preservando os gates existentes.

Retarget real após preparação paralela e reutilização estrita da chegada: intenção superada terminou parada com confirmação completa; vencedora terminou com centro9,125px e projeção inspecionada. Oito comandos,32,361s até término do controlador; rapidez ainda não aceita. Regressão não equivale a continuidade quadro a quadro. O primeiro comando já usou resposta da sessão (correção0,6s); próxima otimização deve qualificar aproximação ampla e evitar perda de informação útil entre intenções, sem outro cache paralelo nem extrapolação entre câmeras. Guia externo28.13.8.

Hipótese de aproximação nativa qualificada em dois ensaios privados: retorno por preset próprio após afastamentos de5,2 e13,3graus, primeira transição visual0,64/0,60s, retorno com localização4,06/3,87s e erros0,64/0,92px. Presets temporários removidos com confirmação; projeções posteriores inspecionadas. Não é medição de clique completo nem recurso habilitado no produto. Associação durável entre destinos nativos e geometria da referência passa a ser candidata prioritária para aproximação inicial; ainda exige propriedade/invalidação, conservação óptica, outro eixo/diagonal e retarget/Parar qualificados. Guia28.13.9.

Leitura opcional de coordenada nativa de zoom adicionada ao adaptador existente, sem normalização ONVIF/ampliação e sem custo novo no polling normal;21 testes aprovados. Retorno diagonal por preset temporário observado em3,78s incluindo localização, erro0,52px e coordenada nativa de zoom conservada. Preset removido. Inventário não oferece revisão/coordenadas do preset: conferência de nome/token não certifica posição imutável. Integração durável e retarget nativo permanecem pendentes (guia28.13.10).

Contrato opcional de conservação de zoom em destinos: absoluto envia apenas pan/tilt mesmo sem zoom/telemetria; preset protegido guarda e compara leitura óptica, com verificação posterior explícita exigida. Padrão da aquisição preservado.150 testes de captura/adaptador aprovados. Ainda não integrado ao clique nativo nem carregado no backend de ensaio; guia28.13.11.

Trecho `VisualNavigator.approach_reference` implementado com orçamento, persistência antes do comando, cancelamento e prova geométrica/óptica após parada.160 testes de navegação/sessão aprovados. Ensaio real desse trecho:4,80s e erro0,06px; Parar pela interface durante recall nativo encerrou sem chegada e removeu preset temporário. Associação durável, seleção no clique e retarget entre destinos nativos ainda pendentes; guia28.13.12.

`VisualNavigator.aim` compõe opcionalmente aproximação nativa e refinamento do alvo final;178 testes de navegação/sessão aprovados. Ensaio real com alvo2graus ao lado do ponto: retorno nativo mais um pulso pan0,12s,6,98s, erro1,22px, zoom nativo conservado, preset removido e imagem alinhada inspecionada. Seleção/persistência de pontos e clique completo ainda pendentes; guia28.13.13.

Rodada encerrada antes de15:50:32UTC para limpeza verificada: processos próprios e listeners8100/5174/9227 ausentes, nenhum MediaMTX remanescente e seis presets temporários removidos. Ledger310/344,34 não usados; Goal não concluída. Próxima etapa é propriedade/persistência dos pontos na preparação e seleção no clique, seguida de aceite completo; guia28.13.14.

### Fundo histórico atualizado na sessão — implementação inicial

O ao vivo possui uma camada temporária de fotografias localizadas, em cinza sob o vídeo colorido, reutilizando o viewport e o shader óptico existentes. Duas observações distintas com avanço de mídia e geometria estável qualificam a atualização; identidade da sessão, intenção, época, movimento e bloqueios continuam obrigatórios. A fotografia é copiada antes da localização assíncrona. O atlas não alimenta o localizador nem modifica panorama oficial, cobertura, calibração ou inversão de cliques.

Há indicação histórica, horário da última atualização e restauração do fundo original. Reconectar preserva as fotografias; trocar câmera/fonte/revisão ou reabrir descarta a camada. Dimensões limitadas, sem fila de fotografias ou player adicional. Atualização do mesmo enquadramento tem frequência limitada e continua exigindo qualificação visual.

Validação offline: 17 testes direcionados e 2 testes em navegador isolado aprovados, tipos e build da extensão aprovados. Verificados composição A → B → A, máscara/emenda, descarte, resposta atrasada durante movimento, restauração, reconexão e reabertura. Não certifica movimento físico, fluidez no produto, memória do processo ou melhora de precisão no mundo real. Aceite com imagens reais e condições adversas permanece pendente, assim como eventual melhoria de referências baseada em observações reservadas. Detalhes operacionais e evidências privadas permanecem no guia externo, seção 29.7.

O descarte de uma candidata invalida também sua resposta assíncrona em voo. Regressão adicional cobre perda e retomada do vídeo na mesma época: resultados anteriores à interrupção não podem compor o próximo par de fotografias confirmadas. Controle negativo, retirando a proteção somente em memória, reproduziu a atualização indevida; com a proteção, o teste passou. Detalhes no guia externo, seção 29.8.

Teste de visibilidade aprovado com o componente montado: ausência de novas localizações durante ocultação, preservação do histórico e rejeição da resposta anterior à pausa; a nova região exige duas observações após a retomada. Isso não mede consumo real de recursos ou minimização do sistema operacional. Guia externo, seção 29.9.

### Recuperação de transição óptica na aproximação nativa

A troca de destino durante deslocamento noturno revelou perda de transição sob borrão e distorção óptica. A aproximação nativa agora pode reutilizar o matcher contextual carregado e a calibração vinculada para estimar a rotação entre dois quadros observados. Essa evidência estabelece apenas deslocamento: não fornece pose de chegada, não usa o alvo pedido e não dispensa nova janela de estabilidade, Stop ou confirmação final. Sem modelo carregado ou suporte independente suficiente, permanece a rejeição.

Validação determinística: 412 testes de localização, estabilidade, navegação e sessão; 380 testes do scanner. No replay integral da falha, a recuperação reconheceu transição e estabilidade onde a versão anterior rejeitava ambos. Aceite físico da correção, resposta rápida ao retarget e avaliação de consumo por ação continuam pendentes. Evidências e orçamento no guia externo, seções 30.3–30.4; não declarar aceite integral por esse replay.

Ensaio real posterior exercitou `calibrated_transition` durante troca de destino: o alvo substituído não publicou chegada; o último alvo foi confirmado com erro de 1,43 pixel em análise de largura 960. A projeção levou 13,4 segundos após o segundo clique, portanto rapidez continua pendente. A ocultação real suspendeu os players e reduziu CPU do navegador; na retomada, vídeo ativo foi observado na amostra de dois segundos e alinhamento na de quatro segundos. Medições locais não certificam hardware menor nem memória em uso longo. Artefato transitório de decodificação na prévia permanece aberto. Detalhes e limitações no guia externo, seção 30.5.

Quando a localização expira um quadro após parada observada, a navegação reutiliza a verificação de continuidade do endpoint para obter imagem nova antes de localizar novamente. Mudança de cena mantém a requalificação completa; falha, ambiguidade e frescor continuam impedindo movimento. Validação: 168 testes de navegação e seis testes direcionados do scanner aprovados. Redução de latência no hardware ainda não medida; guia externo, seção 30.7.

O observador de movimento verifica cancelamento enquanto a análise visual está em andamento e pode solicitar Stop antes de ela terminar. A operação aguarda o encerramento dessa análise e descarta seu resultado obsoleto antes de liberar controle; confirmação física continua obrigatória. Regressão: 661 testes de scanner, navegação e sessão aprovados, incluindo análise deliberadamente bloqueada durante retarget e cancelamento. Latência física dessa alteração ainda pendente; guia externo, seção 30.9.

Essas correções foram exercitadas posteriormente no hardware; ganho global de rapidez ainda não aprovado. A candidata seguinte mantém controle, captura e bloqueio da referência durante a sequência serial de destinos pendentes. Somente recibo de Stop ainda atual e observação recente permitem estender a parada anterior; falha de continuidade mantém qualificação completa. Encerramento, revogação, falhas e cancelamento liberam os recursos. 314 testes de sessão/navegação aprovados; validação física dessa continuidade ainda pendente. Guia externo, seções 30.14–30.18.

Ensaio físico posterior confirmou retenção de controle e continuidade do Stop entre intenções: preparação do segundo destino em 0,481 s, centro final com erro de 1,55 pixel, projeção em 11,01 s. Retorno visível entre 2,987 e 3,219 s após novo clique nesta amostra, ainda acima da meta. A comparação demonstra redução de etapa, não aceite estatístico ou das demais câmeras. Guia externo, seção 30.19.

Parar durante deslocamento com um destino substituto pendente foi exercitado no hardware: apenas o primeiro comando físico ocorreu, o pendente foi descartado e nenhum centro foi confirmado. Estado terminal observado cerca de 1,42 s após iniciar o clique em Parar; esse intervalo não mede isoladamente a parada mecânica. Durante seis segundos posteriores, ambos os vídeos avançaram e nenhum comando antigo reapareceu. Guia externo, seção 30.20; rapidez do retarget e aceite integral permanecem pendentes.

As leituras de estado óptico e inventário do preset agora se sobrepõem, com despacho condicionado a ambas e limpeza aguardada em falha/cancelamento. 465 testes de captura, navegação e sessão aprovados. Ensaio real confirmou o último alvo com erro de 1,82 pixel, mas levou 13,77 s: metadados venceram durante a primeira operação e provocaram liberação/reaquisição dos recursos. Ganho global não demonstrado; separar renovação dos metadados da continuidade do controle é a próxima correção localizada. Guia externo, seção 30.22.

A candidata seguinte renova metadados vencidos sob a mesma posse de controle e captura, sobrepondo a descoberta à observação obrigatória da parada quando possível. Verifica novamente vínculo e controle; falha descarta metadados parciais e o destino pendente. Autorização, automação atual e continuidade visual continuam obrigatórias na próxima intenção. 474 testes de captura, navegação e sessão aprovados, incluindo vencimento durante movimento/Stop, mudança de vínculo e cancelamento. Ainda requer recarga e validação física; guia externo, seção 30.23.

Validação física posterior exercitou o vencimento e a renovação durante retarget, preservando controle/captura e continuidade da parada. Preparação do segundo destino em 0,584 s, contra 2,301 s no ensaio com liberação; projeção final em 13,445 s e erro de 1,91 pixel. A redução dessa etapa está demonstrada, mas observação do movimento e reconhecimento contextual ainda variam e a meta global permanece pendente. Imagens e decomposição no guia externo, seção 30.24.

Reconhecimento antecipado reutiliza o localizador a partir do primeiro quadro visualmente estável, enquanto Stop e observação continuam. Uma análise por câmera, sem fila ou uso de recursos físicos; resultado antecipado não publica pose/chegada. Encerramento drena a análise e retarget não a aguarda. 773 testes passaram. Ensaio físico: localização final de quadro novo em 0,171 s, projeção confirmada em 9,425 s após retarget e erro central de 0,61 pixel. Ainda falta o retarget físico durante essa análise, a meta de reação e o aceite integral. Guia externo, seções 30.27–30.28.

Retarget físico posterior ocorreu com a análise antecipada ainda em andamento: operação substituída não publicou resultado nem reteve a fila física. Último destino confirmado em 8,934 s, erro central de 0,91 pixel; sequência visual preserva as prévias e não mostra projeção do alvo descartado. Reação observável ainda superior a dois segundos. Vídeo e diagnósticos no guia externo, seção 30.29; Parar/fechar durante essa análise e demais critérios seguem pendentes.

Parar durante reconhecimento antecipado também foi exercitado fisicamente: descartou o destino substituto, consumiu somente o primeiro movimento e terminou sem erro ou chegada atrasada. Controlador encerrado aproximadamente 1,29 s após iniciar Parar; não é medida isolada de frenagem. Nos seis segundos posteriores, ambas as transmissões avançaram e nenhum comando antigo reapareceu. Fechamento durante análise, reação do retarget e aceite integral permanecem pendentes. Guia externo, seção 30.30.

Na continuidade entre destinos, descoberta atual e extensão visual da parada agora se sobrepõem sob o controle retido. Ambas continuam obrigatórias; imagem vencida exige Stop/janela completos, e cancelamento drena a observação antes de liberar captura. 490 testes passaram. Ensaio físico reduziu a preparação do segundo destino para 0,339 s, com erro central de 0,89 pixel; projeção em 9,535 s e reação ainda acima de dois segundos. Melhora da etapa demonstrada, aceite global pendente. Guia externo, seções 30.32–30.33.

Clique fora da tolerância de um ponto nativo exercitou aproximação ampla seguida de refinamento: três movimentos, centro confirmado com erro de 11,39 pixels dentro do limite de 12. Projeção em 14,925 s, acima do teto de 12 s para esse deslocamento médio em condição degradada. A primeira correção teve espera adicional antes do envio ao dispositivo; descoberta do failsafe ONVIF é hipótese a medir, não motivo para remover a proteção. Guia externo, seção 30.34; aceite integral permanece pendente.

Perfil somente leitura mediu 0,76 s para descobrir o timeout ONVIF no cliente novo. Preparação antecipada implementada no contexto da própria posse, após Stop inicial aceito e durante a observação: não ocupa fila física, não adia Stop/preset e não remove a verificação obrigatória no pulso. Cancelamento e liberação drenam a leitura; configuração e posse são revalidadas. 475 testes passaram. Ainda requer recarga e comparação física do primeiro pulso; guia externo, seções 30.35–30.36.

Ensaio posterior confirmou preparação antecipada no hardware: executor do primeiro pulso em 0,053 s, praticamente igual ao transporte, contra 0,769 s com preparação no caminho. Timeout independente preservado. Ganho de etapa demonstrado; total de 19,709 s ainda reprova rapidez, com uma correção adicional e aproximação mais demorada nessa amostra. Erro central de 3,90 pixels. Evidências e gravação no guia externo, seção 30.37; não há aceite integral.

Fechamento durante reconhecimento antecipado foi exercitado pela interface: somente um movimento, encerramento parado e nenhum resultado antigo publicado. Reabertura alinhada em 3,689 s, sessão nova com zero comandos e ambas as transmissões avançando nos seis segundos seguintes. O coletor foi corrigido para reconhecer o contrato de sessão encerrada (HTTP 410); a falha de instrumentação não exigiu repetir movimento. Esse ensaio aprova fechamento/reabertura na candidata, sem certificar recuperação de rede, rapidez do retarget ou aceite integral. Guia externo, seção 30.38.

Observação passiva posterior por 50,8 s: 255 amostras mantiveram Vídeo alinhado e ambos os players avançando, sem pausa, erro ou eventos de carregamento. Capturas inicial, intermediária e final examinadas mostram a projeção presente; zero comandos emitidos. Oscilação não reproduzida nessa janela, sem certificar flashes entre amostras, estabilidade prolongada ou perda de rede. Diagnóstico dos refinamentos mostra respostas diferentes para pulsos iguais e custo dominante posterior ao Stop; proteções e limites preservados. Guia externo, seção 30.39; rapidez continua pendente.

Leitura final da posição agora se sobrepõe à geometria e à confirmação das imagens após estabilidade observada. Ambas são exigidas antes de publicar; leitura lenta renova a imagem e falha/cancelamento drena as tarefas. 726 testes aprovados. Ensaio real confirmou o alvo com erro de 10,60 pixels, quatro comandos e projeção em 18,233 s. Os três ciclos de refinamento somaram 0,677 s a menos que na amostra anterior; comparação não isola toda a variação mecânica e a meta de 12 s continua reprovada. Guia externo, seção 30.40, com gravação e diagnósticos; aceite integral pendente.

Retarget na candidata completa confirmou somente o último destino, com erro de 1,25 pixel, dois comandos e projeção em 9,597 s. Reação visível entre 3,062 e 3,293 s nas duas prévias, ainda acima da meta. Decomposição aproxima 2,20 s até aceitação do novo comando; observação passiva posterior do WebRTC mediu recepção até apresentação de 9–10 ms, sem timestamp de exposição disponível. Não atribuir todo o atraso à renderização nem chamar aceitação de comando de movimento físico. Evidências no guia externo, seção 30.41; aceite integral pendente.

Falha real de recuperação: peer WebRTC fechado sem novo evento deixava a prévia indisponível por mais de 45 s. O player compartilhado agora verifica estados terminais no relógio de estatísticas existente, reutilizando recuperação e backoff. 25 testes e tipos aprovados. Na repetição em navegador real, vídeo novo em 1,646 s e alinhamento em 3,421 s, teleobjetiva contínua e zero comandos PTZ. Cobertura restrita ao fechamento isolado da conexão; não certifica perda prolongada de rede ou câmera. Guia externo, seção 30.42; rapidez do controle e aceite integral continuam pendentes.


Retarget com leitura nativa sincronizada confirmou o último destino com erro de 1,17 pixel e projeção em 9,663 s. A posição nativa mudou antes da prévia; reação visível principal entre 3,527 e 3,650 s continua acima da meta. Telemetria declarada não certifica movimento físico nem substitui imagem. Captura ativa já usa decodificador FFmpeg com uma thread; experimento JPEG sintético não demonstrou ganho ao limitar também o encoder. Nenhuma alteração de transporte sem benefício comprovado. Guia externo, seção 30.43, inclui gravação e limites das medições; rapidez e aceite integral pendentes.


Comparação passiva de imagens reais isolou atraso adicional da prévia: hardware 0,483 s, CPU 0,250 s e hardware restaurado 0,442 s nas medianas das séries. O modo por CPU foi apenas um ensaio reversível por saída, restaurado com readback; ganho de 0,19–0,23 s não certifica reação mecânica nem justifica padrão global. Consumo do publicador e falha/correção do coletor estão no guia externo, seção 30.45. Nenhum comando PTZ adicional.

Persistência de confiança do encoder agora evita regravar e sincronizar registros idênticos a cada quadro. Mantém transições, quarentena, histórico e nova tentativa após falha de escrita; 22 testes de publisher/chaos aprovados. Candidata no checkout, ainda requer recarga para validação na instância. Guia externo, seção 30.46; rapidez e aceite integral continuam pendentes.


Candidata de persistência carregada após recarga autorizada. Reabertura alinhada com sessão nova, zero comandos e ambos os vídeos disponíveis. Em 6,065 s, os dois publicadores avançaram 88 quadros cada e o arquivo de confiança manteve o mesmo mtime: gravações redundantes cessaram no caminho real. Isso não certifica economia integral de recursos nem velocidade do PTZ. Guia externo, seção 30.47. A seção 30.48 registra hipótese de calibração nativa avaliada offline, ainda sem uso para comandos; aceite integral pendente.

Validação histórica ampliou a hipótese nativa para 89 pares em 26 operações, incluindo os dois sentidos. A relação local sustenta investigação de calibração, mas resíduos acumulados e ausência de pares temporalmente vinculados de orientação completa impedem seu uso direto como destino. Nenhum novo comando ou alteração do controlador nesta análise. Guia externo, seção 30.49; velocidade e aceite integral permanecem pendentes.

Diagnóstico limitado existente agora preserva rotação completa, identidade/tempo da imagem e intervalo das consultas de posição, sem novas consultas ou autoridade de controle. 889 testes aprovados. Percurso real curto confirmou centro com erro de 1,52 pixel, mas projeção em 13,94 s ainda reprova rapidez. A coleta expôs posição anterior à imagem renovada e ausência do último registro no manifesto; a gravação final do resultado foi corrigida para incluir os registros atualizados, com 167 testes de sessão aprovados, ainda aguardando recarga dessa última correção. Guia externo, seção 30.50, com gravação e limites de qualificação; aceite integral pendente.

Persistência final carregada e comprovada no retorno real: último quadro localizado presente no resultado. Retorno curto usou um preset, erro de 8,14 pixels contra o clique e projeção em 9,75 s; precisão dentro da tolerância, rapidez ainda reprovada. Modelo de eixo avaliado offline previu ponto intermediário separado do ajuste com 0,36 pixel e três pontos atuais usando referências históricas com 1,16–1,60 pixel. Isso não qualifica comando absoluto, dinâmica ou histerese; nenhum parâmetro experimental instalado no controlador. Guia externo, seção 30.51; aceite integral pendente.

Expiração durante a medição final agora reutiliza a renovação de continuidade já existente, mantendo retorno à estabilidade integral quando necessário e rejeição da imagem antiga. 348 testes aprovados; candidata carregada. Clique médio real terminou com erro de 6,84 pixels e projeção em 14,88 s, ainda acima da meta. O ensaio não teve expiração, portanto não comprova economia temporal da correção. Modelo geométrico previamente ajustado previu o novo endpoint de pulso reverso com 1,26 pixel, mas continua sem autoridade de atuação. Guia externo, seção 30.52; aceite integral pendente.

Instrumentação real separou a espera da projeção: controlador em 6,27 s, imagem alinhada em 8,71 s, com 0,77 pixel de erro. Primeira localização do navegador levou 2,01 s e expirou; nova imagem localizou em 95 ms. Candidato offline ajustou correspondências originais do backend ao raster do navegador em 66 ms, contra 1,41 s do caminho normal, com orientação próxima e três negativos sintéticos rejeitados. Ainda não implementado ou qualificado para origem, época e ambiguidade; não é ganho de produto certificado. Guia externo, seção 30.53; rapidez e aceite integral pendentes.

Rodada diurna de fechamento do MVP: chegada com um comando, erro de 1,88 pixel e projeção em 8,60 s; troca de destino durante movimento confirmou somente a última intenção, erro de 1,84 pixel e projeção em 8,29 s após o segundo clique. Prévia durante movimento e projeção final examinadas em capturas e gravação. Redimensionar, minimizar, arrastar e usar zoom/scroll da visualização não emitiram comandos PTZ. Reconexão alinhada em 2,69 s; retorno de aba realmente oculta em 3,46 s, após retirar interferência da emulação de foco do instrumento. 36 testes de interface aprovados. Guia externo, seções 30.56–30.57, registra orçamento renovado e prioridade ao MVP. Caminhos funcionais comprovados não encerram as metas de velocidade, qualificação entre câmeras ou consumo global.

Observação do navegador agora pode reaproveitar pares originais de referência de outra captura qualificada, refazendo a geometria sobre os pixels atuais. Mantém identidade, validação independente, ambiguidade, frescor e recuperação; não copia pose nem cria cadeia de imagens. 238 testes aprovados e candidata carregada. Chegada real: projeção em 7,07 s, apenas 0,36 s após o controlador, erro de 3,12 pixels. Retarget: projeção em 6,82 s após o segundo clique, intervalo de 0,21 s após o controlador, erro de 2,68 pixels. Observação posterior de 45 s manteve alinhamento e ambos os vídeos avançando, sem eventos de carregamento nas 225 amostras. Guia externo, seção 30.58, com gravação e limites: reação mecânica, distribuição das metas temporais e performance integral continuam pendentes.

Análise da gravação localizou a reação visível ao retarget entre 2,93 e 3,11 s, ainda acima de 2 s; inclui transporte e amostragem. Os registros confirmam controle retido, parada observada e preparação do novo comando, sem comprovar espera removível com segurança. Garagem também abriu com vídeo alinhado pelo mesmo seletor, apenas uma fonte e sem teleobjetiva artificial; nenhum movimento realizado nessa câmera. Frente restaurada alinhada e parada, sem comandos na troca. Guia externo, seção 30.59; não equivale a qualificação mecânica da Garagem ou aceite completo.

Atividade em segundo plano medida sem emulação de foco: requisições próprias caíram de 34 para zero na janela de aproximadamente 15 s; ambos os vídeos pararam de avançar, CPU do Chrome dedicado caiu de 22% para 0,5% de um núcleo e retorno alinhado ocorreu em 2,44 s. Player normal exercitado com a fonte principal e preferências restauradas. Seu consumo de 7,2% no Chrome usava um vídeo de 1920 pixels, enquanto a panorâmica usava dois de 1280 pixels; a diferença não isola o custo da projeção. Backend manteve consumo de base próximo de três núcleos nos cenários oculto/normal, com outros trabalhos ativos. Guia externo, seção 30.60, preserva medições, limites de memória e pendências; nenhum pipeline foi alterado para melhorar os números.

Três intenções reais A→B→C: B e C chegaram com 4 ms de intervalo; somente A e C geraram operações físicas. C foi confirmado com erro de 2,37 pixels, dois comandos totais. Fundo temporário limpo e recomposto sem interromper alinhamento; 60 arquivos oficiais permaneceram idênticos por SHA256. Movimento manual externo à sessão retirou a projeção antiga em 1,13 s e realinhou em 2,15 s, sem retorno automático ao alvo anterior. Esse ensaio usa o controlador Toposync e não prova atuação via aplicativo do fabricante. Mais 16 testes geométricos aprovados. Guia externo, seção 30.61, e auditoria de MVP preservam as pendências de aceite integral.

Clique fora do campo atual, em região coberta da panorâmica, foi confirmado em ensaio real: percurso reconstruído de 51,76°, dez comandos, projeção em 34,29 s e erro de 8,14 pixels na análise de 960 pixels. A geometria inicial comprovou que o alvo estava fora do vídeo. Os ciclos de movimento/parada somaram 25,53 s; localização, 6,05 s. O caminho funcional está demonstrado, mas a rapidez de percurso longo continua reprovada; resultados próximos a referências nativas não representam toda a panorâmica. Guia externo, seção 30.62, registra gravação, diagnóstico e limite da conclusão. Nenhum limiar de segurança alterado.

Preparação nativa existente validou uma referência no novo ponto com saída e retorno reais, sem alterar controlador ou tolerâncias. O mesmo clique longo passou de 34,29 s e dez comandos para 7,76 s e um comando, com erro de 7,33 pixels. Três chegadas entre pontos preparados tiveram mediana de 7,22 s e máximo de 7,76 s; o resultado não cobre primeira visita ou regiões arbitrárias. Reação inicial visível permaneceu entre 2,91 e 3,27 s, ainda acima da meta. Guia externo, seção 30.63, registra gravação e qualificação: preparar referências na aquisição é útil, mas não encerra o aceite de rapidez ou retarget.

Novo clique durante movimento deve substituir o destino anterior, mantendo apenas uma intenção pendente e uma operação física. O fluxo serial existente e o teste real A→B→C cumprem essa regra; a reação ainda precisa atingir a meta temporal. Ensaio atual do botão Parar, acionado após movimento detectado por imagem, terminou sem alvo pendente ou resultado antigo em 1,56 s; projeção voltou em 3,57 s. O tempo do controlador não isola o instante físico da parada. Guia externo, seção 30.64, reafirma o requisito do usuário e preserva essa distinção.

Consulta de estado atrasada que falha após novo clique agora verifica sessão e sequência antes de limpar a apresentação ou mostrar erro. Regressão reproduziu a falha anterior; nove testes de polling/ordenação/época e typecheck passaram. Falhas atuais continuam visíveis e o watchdog foi preservado. Reabertura real voltou alinhada e parada, sem comandos físicos. Guia externo, seção 30.65; mudança protege a UX da última intenção, sem alegar ganho mecânico.

Clique já centralizado deixa de selecionar retorno nativo redundante quando a dica visual recente indica tolerância atendida. A dica só escolhe o caminho: chegada continua dependendo de imagem nova, parada e medição independente. 345 testes passaram. Após recarga, repetir o mesmo clique confirmou centro em 2,36 s, erro de 3,32 pixels e zero deslocamentos; nenhuma detecção de movimento nas 18 amostras. O caso não entra na distribuição de rapidez mecânica. Guia externo, seção 30.66.

Cancelamento pode reutilizar um recibo de Stop recente e ainda vigente sob a mesma posse, evitando envio duplicado; a janela visual de parada continua obrigatória. Default dos demais fluxos preservado e falha de qualificação conserva novo Stop. 745 testes passaram. Retarget real confirmou só o último alvo com erro de 3,32 pixels; reação visível entre 2,88 e 2,98 s continua acima de 2 s, sem ganho perceptível comprovado. Guia externo, seção 30.67, contém gravação e limites da comparação.

Falhas conhecidas de apontamento receberam mensagens compreensíveis na interface: expiração, perda de controle, rota/chegada não qualificadas e referência salva alterada, entre outras. Nenhum gate ou comportamento físico modificado. Typecheck aprovado; sessão real permanece alinhada e parada, sem novo comando. Guia externo, seção 30.69; testes de falhas físicas não são inferidos da tradução dos códigos.

Janela ampliada de recursos: aproximadamente 92 s visíveis com 91/91 amostras alinhadas e ambos os vídeos avançando; aproximadamente 92 s ocultos com zero consultas próprias e vídeos suspensos. Chrome dedicado caiu de 22,5% para 0,5% de um núcleo; retorno alinhado em 3,04 s, sem comandos PTZ. Backend teve outros processos mudando durante a coleta, portanto sua variação não foi atribuída integralmente à panorâmica. Guia externo, seção 30.71, mantém série de memória e limites da medição.
Resposta atrasada da mesma intenção não deve desfazer uma chegada já confirmada nem voltar a mostrar movimento. A proteção foi acrescentada à aceitação de estados da interface, mantendo novas intenções e bloqueios prioritários; 11 testes determinísticos de ordenação, consulta e épocas passaram. A extensão de câmeras exige compilação própria (`npm run build:extension-ui -- cameras`); recarregar apenas o frontend principal não comprova que seu código atualizado foi servido. Pacote recompilado e navegador reaberto, mas a repetição do ensaio de resposta atrasada com esse pacote permanece pendente. Não atribuir à correção evidências produzidas com o pacote anterior.

Em 27/09/2026, após autorização para interação mecânica, o ensaio pontual da Frente Reolink enviou somente um Stop pela API. Recibo aceito em 0,126 s, estado físico IDLE e imagens diretas antes/depois disponíveis; não houve deslocamento comandado. Posição permaneceu desconhecida e `geometry_safe=false`: retorno, precisão e parada a partir de movimento não foram certificados. A revisão `0bb97cd4`, em processo isolado com uma fonte direta a 1 quadro por segundo, entregou snapshot com frescor do decodificador em 2,80 s; evidência de captura física continuou não verificada. O serviço preexistente retornou 503 e sua revisão carregada não foi qualificada. Diagnóstico e recibos privados permanecem no guia externo da Goal e em seus artefatos; esse ensaio não substitui os aceites de cobertura ou movimento.

A recuperação do backend restaurou a disponibilidade, mas a inspeção visual reprovou a ingestão centralizada da Frente: a repacotização produziu corrupção, também reproduzida no candidato MediaMTX v1.21.1. Somente `wide_main` passou ao modo direto já suportado e entregou imagem íntegra. A referência panorâmica anterior ficou incompatível com a identidade da fonte; nenhum contrato de identidade foi relaxado. A verificação nativa de controle terminou antes da aquisição de posse com `external_automation_active`, sem ensaio de movimento ou retorno. A API agora reutiliza o diagnóstico conhecido, em vez da mensagem genérica; a interface já traduzia esse código. Permanecem pendentes exclusividade física, referência compatível e os aceites de movimento e cobertura.

Em novo ensaio autorizado de 27/09/2026, a Frente confirmou rastreamento nativo ativo e retorno automático desligado. O rastreamento foi suspenso temporariamente por controle Reolink documentado na implementação de referência; leitura completa confirmou que somente esse campo mudou. A verificação de produção terminou em 32,07 segundos: pan positivo e retorno visual aprovados, pan negativo sem resposta visual suficiente, demais verificações não alcançadas. O controlador confirmou enquadramento restaurado; Stop adicional foi aceito e a configuração nativa original foi restaurada por igualdade completa. O resultado integral permanece reprovado.

A análise offline dos replays preservados confirmou deslocamento de 10,31 pixels no primeiro movimento e 0,029 pixel nos 137 quadros da tentativa rejeitada, na análise de 960 pixels. O transporte dessa tentativa consumiu 133 milissegundos frente ao pulso solicitado de 120, deixando zero tempo remanescente. Isso sugere investigar duração efetiva e resposta mecânica; não comprova causa única nem justifica reduzir tolerâncias. Nenhuma nova captura panorâmica, alteração de limiar ou qualificação integral de cobertura foi realizada.

A verificação passa a usar pulsos de 300 milissegundos, duração já usada na busca de referência do scanner, em vez do piso de microcorreção de 120 milissegundos. O transporte continua descontado do orçamento, sem reiniciar a duração após a resposta. Permanecem doze verificações, reserva de retorno antes de mover, retorno visual após cada movimento e interrupção na primeira falha. Dezoito testes focados passaram, incluindo retorno indisponível, retomada sem repetição de comandos e resposta lenta. Esse resultado é de software; o ensaio físico anterior de 120 milissegundos permanece reprovado e não qualifica a nova duração.

O ensaio real de 300 milissegundos, com a revisão `c1e0cd76`, terminou em 52,12 segundos. As quatro direções passaram uma vez com retorno confirmado; na segunda repetição de pan positivo o retorno falhou. O destino salvo deixou erro de 4,02 pixels e a única correção visual aumentou o erro para 7,75 pixels, provocando interrupção por `visual_error_increased`. Resultado integral reprovado: quatro de doze verificações aprovadas, estado físico parado, enquadramento inicial não confirmado. Stop foi aceito e os parâmetros nativos foram restaurados por igualdade completa; isso não equivale a restaurar a posição. Nenhum limiar foi reduzido ou novo retorno disparado para encobrir a falha. A próxima investigação deve usar o replay da correção e sua direção/resposta antes de autorizar outra tentativa pelo controlador.

A análise offline do retorno reprovado encontrou divergência entre a previsão da sondagem inversa (50 milissegundos) e o comando enviado para direção ainda não medida (80 milissegundos). A seleção agora prevê os mesmos 80 milissegundos, considera o lado em que o retorno nativo deixou a imagem e recusa a sondagem genérica quando a evidência de saída não prevê melhora em nenhuma direção. A evidência continua sendo uma hipótese de direção, não calibração de ganho. Nos dados capturados do retorno reprovado, a decisão corrigida não despacha a microcorreção que piorou o erro. Os 180 testes do módulo de navegação passaram; duas regressões cobrem ultrapassagem e ausência de fallback cego. Não houve novo movimento físico. Retorno preciso permanece não qualificado: a proposta de velocidade menor calculada pelo planejador existente exige comprovação da resposta real antes de uso ou aceite.

Uma leitura ONVIF do perfil exato da Frente declarou espaço contínuo normalizado de −1 a 1, sem precisão física ou posição absoluta verificável. Ensaio pontual pela API existente reservou e conferiu um preset próprio, suspendeu o rastreamento nativo e enviou uma única sondagem de 80 milissegundos a velocidade 0,05. O transporte consumiu 68 milissegundos; restaram 12 milissegundos do orçamento. Imagens íntegras antes/depois mostraram apenas 0,31 pixel de diferença, insuficiente para qualificar uma resposta fina útil. Após uma tentativa de retorno e Stop, a imagem ficou a 0,28 pixel da referência desta rodada; isso não certifica repetibilidade nem restaura retrospectivamente a referência do ensaio anterior. Parâmetros nativos restaurados por igualdade completa. O preset temporário foi removido com leitura posterior e preservação exata dos demais. A resolução fina continua não qualificada; nenhuma resposta linear de velocidade/tempo foi promovida a calibração.

Na retomada de 27/09/2026, o diagnóstico offline confirmou que tanto o deslocamento mediano quanto o percentil 95 pioraram na correção rejeitada. Dois pares estáticos preservados apresentaram cerca de 0,014 e 0,013 pixel; a diferença entre métricas não explica aquela falha. Dez testes focados de orçamento, velocidade não qualificada e recusa de ultrapassagem passaram. O novo ensaio mudou a variável para uma sondagem de 200 milissegundos a velocidade 0,025, com retorno reservado. Uma falha de consulta nativa encerrou a primeira preparação sem movimento ou alteração; nova leitura instrumentada confirmou Login/GetAiCfg/Logout antes de uma única retomada. O pulso deslocou 22,01 pixels, portanto não qualificou controle fino. Retorno e estabilidade visual foram confirmados nesta rodada, com erro de 0,078 pixel; não é prova de repetibilidade. Stop aceito, configuração nativa restaurada e somente o preset temporário removido, preservando os demais. A API manual usada não mudou entre o backend carregado em `c1e0cd76` e o checkout `0d0a9294`; não houve reinício nem uso do planejador novo durante a sondagem. Duração do motor e ganho entre velocidades continuam desconhecidos; reduzir velocidade nominal não demonstrou reduzir o passo ao necessário para corrigir o resíduo anterior. Nenhuma tolerância foi relaxada.

A segunda sondagem manteve 200 milissegundos e reduziu somente a velocidade para 0,005. O deslocamento foi maior, 67,80 pixels; transporte de 72,8 milissegundos e saldo de 127,2 milissegundos não explicam a duração física do motor. Uma única tentativa de retorno reservado terminou a 6,75 pixels da referência, reprovando o limite de 3 pixels. Pares estáticos antes, depois e após retorno ficaram abaixo de 0,02 pixel; imagens foram inspecionadas e estavam íntegras. Stop aceito e configuração nativa restaurada por igualdade completa, mas posição de retorno não confirmada. O preset próprio foi preservado para diagnóstico, sem nova chamada ou correção cega. O orçamento acumulado registra dois deslocamentos e dois retornos; a falha de retorno encerra a rodada mesmo com saldo disponível. Esses dados refutam usar uma redução nominal de velocidade como calibração linear; não identificam isoladamente causa mecânica, temporização ou interpretação pelo firmware. Retomar movimento exige resolver a condição de retorno e estabelecer hipótese verificável nova, mantendo bloqueados cobertura e seguimento que dependem desse controle.

## Integração da interface — 02/10/2026

Após a imagem salva, a interface conduz à seção de mapeamento da mesma câmera e à consulta de disponibilidade da panorâmica ao vivo. Câmeras impedidas mostram o motivo de incompatibilidade no seletor. A remoção da imagem ativa invalida a continuação; uma imagem disponível continua sem certificar cobertura ou retorno físico. O texto inicial deixou de prometer uma captura limitada a duas faixas.

Verificação focal: transições, ordenação de estado, epoch e traduções aprovados; aplicação renderizada com replay explícito de imagem salva, candidata reprovada e geometria incompatível, sem comandos físicos. Retorno preciso e cobertura conservam os resultados e impedimentos anteriores. Registros internos desta rodada ficam no diretório externo `~/.codex/task-artifacts/toposync/integrated-goal-20260926/ux-integration/`.

## Remoção do editor legado — 04/10/2026

O elemento de câmera mantém duas entradas: calibração pela panorâmica salva e mapeamento do chão com geometria métrica. O editor antigo de calibração, seu diálogo de poses e auxiliares privados sem consumidores foram removidos; os componentes ativos e seus controles de evidência permanecem iguais. Esta simplificação não altera os critérios de cobertura, precisão ou retorno físico.

No mapeamento do chão, imagem e planta passam a ocupar uma coluna em telas estreitas. Os marcadores acompanham o redimensionamento da imagem, inclusive suas faixas vazias, e os campos permanecem dentro dos cartões. Fonte, parâmetros medidos da lente e validação continuam acessíveis; a adaptação de layout não modifica pontos salvos nem aprova geometria.


## 08/10/2026 — preparação opcional e nova validação física

A captura agora tenta preparar uma referência de aproximação rápida somente depois de publicar uma panorâmica qualificada e confirmar o retorno. A tarefa de captura é a proprietária dessa preparação; leitura de estado e reabertura da página não iniciam movimento. Falha da referência não invalida a imagem, e uma referência de candidata continua vinculada àquela imagem e revisão. Interrupções não reiniciam essa preparação automaticamente.

A interface diferencia incompatibilidade da panorâmica, conflito de revisão, permissão e conexão. A preparação manual fica em uma seção recolhível. Botões indisponíveis têm cursor e aparência próprios, sem animação de interação; transformações de posicionamento dos marcadores são preservadas.

Ensaios físicos realizados na instância reiniciada, sem alterar a configuração:

- Frente (`b1faec4d073a40159e71bb5a9ff21844`): preparação bloqueada por rastreamento automático ativo, confirmado por leitura do equipamento. Nenhuma captura qualificada.
- Garagem (`13ab2adb2609419a8581154c741e30e9`): movimento observado; parada confirmada. Retorno não aprovado: erro final de 17,08 pixels na análise de largura 960, acima do limite de 3 pixels. O pulso corretivo de 80 milissegundos cruzou a referência e aumentou o erro; não houve repetição cega nem relaxamento de limites.
- Os catálogos de posições preexistentes foram comparados antes/depois e preservados nas duas câmeras.

A integração de software não certifica aproximação rápida física, nova panorâmica nem mapeamento real na composição. Permanecem pendentes: liberar o controle exclusivo da Frente, resolver e qualificar o retorno da Garagem, gerar panorâmicas compatíveis, comparar tempos de aproximação e concluir seis pares de ajuste mais dois de conferência reais.


## 09/10/2026 — proteção comum e evidência do retorno

A aquisição do adaptador agora atualiza observações e exige a mesma qualificação de automações usada pela navegação e pela preparação de referência. Estado desconhecido sem confirmação explícita impede nova aquisição; automação ativa prevalece sobre confirmação salva. A limpeza de referências continua verificando propriedade/vínculo e não precisa adquirir controle de movimento. Stop permanece independente desse bloqueio.

Recibos registram início/fim monotônicos da execução; Stop automático tem motivo, vínculo e resultado próprios, associados ao diagnóstico do scanner. Esses tempos não são timestamps físicos do motor. Uma reserva limitada a um replay adicional por trabalho preserva a correção reprovada depois da estabilização, mesmo com os replays ordinários já preenchidos; o identificador não substitui o replay original da observação.

Validação: 1.258 testes da bateria de captura/controle/navegação/scanner/referências/fonte aprovados; verificações focais posteriores cobriram exposição do recibo do watchdog, limpeza sem aquisição, mensagem de automação desconhecida e identificação do replay reservado. Typecheck, compilação da extensão e diff-check aprovados. A compilação mantém o aviso existente de tamanho dos pacotes.

Leitura nativa atual da Frente ainda reportou rastreamento ativo. Nenhum movimento novo foi executado nesta rodada. A qualificação física do controle, a nova panorâmica e a calibração real permanecem pendentes de condições operacionais verificadas; esta etapa não certifica hardware nem altera os limites de precisão.

### Continuação após confirmação operacional — 09/10/2026

Após o usuário ajustar a Frente, a leitura nativa confirmou rastreamento e retorno automático desligados. A confirmação humana atual da Garagem foi registrada pela API, sem outras alterações de configuração nessa operação.

O ensaio pode agora limitar explicitamente a quantidade de verificações; uma rodada limitada não certifica o controle completo. As primeiras rodadas de um deslocamento/retorno passaram nas duas câmeras. Na sequência completa, a Frente passou em 12/12 verificações, com erro máximo de 2,637 pixels. A Garagem passou em duas e encerrou na terceira: retorno vertical com erro final de 6,420 pixels e resposta insuficiente ao pulso corretivo de 80 ms. Parada confirmada, retorno não confirmado; replay da correção preservado. Nenhuma captura nova da Garagem foi iniciada.

A Frente produziu 21 imagens na captura `6604e5bea7b24ee39d52d60f1a5e45e0`, confirmou retorno e preparou automaticamente a referência rápida `11f4a33794a3554b86e1ceafb52c575e`. A imagem `a1e8cb95659c46198e1a76d10e1d5c13` foi inspecionada e adotada pela interface; a anterior foi preservada. Cobertura permanece parcial, sem certificação do alcance total. A referência permaneceu pronta após reinício; aceleração em trajetórias comparáveis ainda não foi medida.

O wizard passou a enquadrar a área fotografada a partir da máscara também em rascunhos importados. A revisão dos pares ficou abaixo das imagens e a altura desktop foi ajustada para evitar que o rodapé cortasse a região de marcação. Conferência visual real em desktop e largura 390: sem transbordamento horizontal, alvos de pelo menos 44 pixels e estado desabilitado perceptível. Regressões focais: 8 testes do scanner, 5 da API de verificação e 45 da API de mapeamento aprovados; typecheck e build aprovados.

A alternativa de retorno por posição nativa da Garagem não chegou a movimentar: oito posições ocupam os oito espaços anunciados, e a criação não foi confirmada. O catálogo anterior foi preservado. Liberação de uma posição preexistente aguarda autorização específica. O rascunho da Frente permanece sem pares até esclarecer a correspondência da casa com a planta; calibração real e entrega integrada continuam pendentes.

### Prévia utilizável da Garagem — 09/10/2026

Após o pedido explícito de visualizar uma panorâmica mesmo com retorno impreciso, uma aquisição limitada partiu do enquadramento atual estabilizado, usando a política existente e sem alterar tolerâncias. A captura `49446549149a4bc399b6f619f6cb63f2` preservou 19 fotografias, incluindo expansão inferior e ambos os lados. Duas ligações locais recusadas limitaram a cobertura; não foram aprovadas por exceção.

A reconstrução `1146e50460ff44d093ddee52fa7faadc` passou na avaliação geométrica (percentil 95 do erro reservado: 1,349 pixels na largura 960). A imagem foi inspecionada, adotada pela interface e aberta no wizard da Garagem, compatível com a fonte atual. Mostra portão, carro e piso; tem cobertura parcial e marcas de horário repetidas na borda superior. A imagem anterior permanece salva, os oito presets não mudaram e as composições permanecem iguais ao registro inicial desta tarefa.

O retorno terminou com parada confirmada e erro visual de 6,273 pixels, acima dos 3 pixels exigidos. Quatro correções reduziram o erro de 45,272 pixels até esse resultado, esgotando o orçamento existente. A resposta variável aos pulsos curtos permanece uma limitação observada, sem diagnóstico mecânico conclusivo. A aproximação rápida foi corretamente omitida por retorno não confirmado. A prévia pode ser marcada no wizard, mas não certifica retorno preciso, cobertura completa ou calibração real dos pares na composição.

### Simplificação dos avisos e entrada no mapeamento — 09/10/2026

Uma imagem compatível e qualificada com limitações passa a apresentar um aviso discreto, com explicações recolhidas sobre cobertura, retorno e aproximação rápida. Parada não confirmada, imagem incompatível e qualidade reprovada continuam visíveis. Candidatas sem avaliação de qualidade são explicitamente apresentadas para revisão, inclusive no recorte.

O wizard inicia pelo entorno da câmera, ajustando a escala à dimensão do painel. A extensão inicial da vista usa o componente genérico existente e não altera coordenadas, geometria ou pontos. A troca entre pares preserva a navegação; uma marcação ainda vazia não provoca mais mensagem de recuperação de conteúdo.

Validação de software: oito cenários de navegador aprovados em ambiente isolado, cobrindo seis pares de ajuste e duas conferências, ativação, reabertura, correção/desfazer, recortes 2D/3D, tela estreita em português e distinção entre limitações da imagem e parada incerta. Typecheck do frontend e da extensão, build da extensão e diff-check aprovados; permanece o aviso de tamanho dos pacotes. O simulador de fonte foi atualizado ao contrato existente de limpeza de referências gerenciadas.

Conferência visual com a panorâmica real da Garagem em larguras de 320, 768, 1024 e 1440 pixels, sem transbordamento horizontal. Nenhum novo movimento ou captura física nesta etapa. Composições preservadas. A validação sintética não substitui a identificação e conferência dos pares físicos da Garagem, ainda pendentes.

### Diagnóstico e revisão do mapeamento — 10/10/2026

O estimador passa a refinar candidatos antes de rejeitar o consenso angular. Os limites de seis ajustes consistentes, 80% de consenso e 1° continuam iguais, e as conferências não participam do ajuste. Candidatos recusados conservam diferenças por ponto para diagnóstico, sem publicar projeção utilizável.

O assistente mantém navegação de etapas e pontos visível acima das imagens, remoção próxima do ponto selecionado, conferências A/B, estados distintos para salvo e validado, diferenças angulares e acesso à revisão mesmo com ativação bloqueada. A ação desabilitada tem estilo e explicação visíveis. Rotação da composição, vistas, ampliação, deslocamento, expansão, teclado, toque, recuperação e substituição do ativo foram preservados.

Validação de software: 38 testes de geometria e 45 da API aprovados; 31 cenários de navegador aprovados, sem repetição automática nem testes intermitentes na rodada aceita. Verificações de tipos do frontend e da extensão, recortes espaciais, compilação da extensão e documentação em inglês e português aprovadas. Permanece o aviso existente de tamanho dos pacotes. Testes de navegador usam fonte simulada; não qualificam hardware.

Na instância real, os mesmos oito pontos do rascunho da Frente foram reavaliados, revisão 20 para 21, sem mudar suas coordenadas ou a composição. Os seis ajustes concordam dentro de 1°, mas as conferências A e B divergem 4,54° e 1,91°. O bloqueio agora informa essas pendências e oferece acesso direto aos pares. A divergência não estabelece se a causa está na marcação, nas medidas, no plano do piso ou na reconstrução da panorâmica. Nenhuma nova captura, movimento físico ou ativação foi realizada. A jornada real completa e a precisão física continuam pendentes.

### Pontos fantasmas como exploração principal — 10/10/2026

O assistente agora abre a exploração bidirecional após os seis ajustes e conserva previsões utilizáveis mesmo quando as conferências reprovam. A prévia de inspeção é identificada como provisória e tem elegibilidade própria; os critérios de ativação e a projeção em uso não foram relaxados. Geometria inválida, fonte incompatível, máscara ausente e revisão desatualizada continuam bloqueando a previsão. Marcar conferências independentes oculta as sugestões; a exploração pode ser retomada pela segunda etapa.

As imagens ganharam espaço, o diagnóstico fica abaixo delas e o estado da prévia aparece junto da instrução. Hover, setas e toque em exploração consultam o cálculo sem criar pares; arrastar continua navegando. Rotação da composição, ampliação, deslocamento, expansão e contexto entre etapas foram preservados.

Validação: 84 testes de geometria/API e 33 cenários de navegador aprovados, incluindo exploração com conferências reprovadas, rejeição da mesma solução pelo projetor de uso normal, quatro rotações, teclado, toque, máscaras, revisões, ativação simulada, recuperação, idiomas, ingress e recortes 2D/3D. Verificações de tipos, recortes e compilação da extensão aprovados; documentação compilada nos dois idiomas. Permanece o aviso de tamanho dos pacotes.

Na Frente real, a previsão foi observada nos dois sentidos com o rascunho da revisão 21 ainda reprovado. O cursor sobre A na panorâmica produziu uma posição na composição; a consulta na composição produziu o marcador na imagem. Os hashes da configuração e do rascunho permanecem iguais aos anteriores à recarga. Nenhuma nova captura, movimento físico ou ativação foi executada. Essa inspeção comprova disponibilidade da interação, não precisão física do mapeamento.

## Primeira etapa dos itens 11 e 12 — 10/10/2026

Configurações passam a resumir referência visual, mapeamento e posicionamento por fonte/composição, com revisão validada pelo serviço ativo. Contagens antigas não certificam panorama; fontes ópticas distintas e seleção de elemento são preservadas. A identidade óptica continua disponível em informação avançada.

Nova publicação guarda resposta causal local vinculada à geometria final. Reconstrução não prepara mais presets nem inicia movimento adicional. A interface dispensa preparação de pontos; navegação ao vivo não consome os antigos. Limpeza explícita e parada de recursos gerenciados permanecem disponíveis. Zoom óptico fixo, localização atual, orçamento e conferência independente de chegada são preservados.

Existe suporte separado para referência métrica e resolução canônica de alvos sem movimento. Atenção não retorna silenciosamente a presets antigos diante de panorama ativo. A migração completa de execução da atenção permanece pendente do contrato físico qualificado.

Estado de aceite: implementação parcial dos itens 11/12. Modelos candidatos de resposta não estão habilitados nas câmeras reais. Avaliação numérica dos registros históricos não demonstrou domínio preditivo qualificado; não houve nova captura, movimento, benchmark de hardware ou certificação de ganho nesta implementação. Cobertura visual e reconstrução pronta continuam distintas de cobertura física aceita. Registros internos e resultados detalhados permanecem fora do repositório.

A criação privada de pontos nativos também foi retirada; a manutenção explícita conserva os registros existentes. No vídeo espacial 2D/3D, uma referência panorâmica selecionada impede a restauração de geometria antiga, mesmo quando inválida. As demais câmeras conservam suas camadas independentes. Verificador espacial, tipos e regressões de manutenção/API/navegação aprovados.


## Estado atual — migração de atenção e consumidores, 10/10/2026

A migração de software dos itens 11/12 está concluída para a jornada panorâmica.
Perfis novos selecionam fonte, elemento/composição, região explícita e retorno
mapeado. O operador usa o perfil; API, arbitragem, acompanhamento em simulação,
prioridades, tempos, pausa, cancelamento e recuperação usam o alvo panorâmico.
Perfis antigos permanecem para revisão explícita, sem inferência de destinos,
reescrita em leitura ou comandos durante a migração. O painel está registrado em
Configurações. Fontes ópticas e dados métricos preservam seus próprios contratos.

O despacho visual reutiliza a concessão de atenção, sem segunda aquisição nem
criação de presets. Aceite exige destino/comando/concessão/isolamento exatos,
chegada visual recente de até 12 pixels, retorno de até 3 pixels, parada e época
de movimento correspondentes. Mudança de revisão, captura vencida ou tomada de
controle interrompem a operação. Zoom permanece fixo. O caminho de navegação
não aceita parâmetros de atalhos por pontos nativos; manutenção explícita dos
recursos antigos continua restrita à propriedade comprovada.

O vídeo espacial e as áreas oferecidas pelo editor respeitam a referência
panorâmica selecionada, sem recuperar camadas antigas quando inválida. A ação
explícita de preservação métrica copia registros completos de lente/pose/solo
para `metric_calibrations`, sem alterar os originais ou substituir referências
já escolhidas. Referência vazia explícita é autoritativa. Nenhuma altura ou pose
tridimensional é inferida da panorâmica.

Validação: bateria integrada de 1.143 testes aprovada; após reforçar captura
recente e intervenção durante navegação pendente, 116 testes de atenção
aprovados. Três cenários de navegador cobrem criação em simulação em 1440/390
pixels, bloqueio de modo físico e migração explícita de perfil antigo. Tipos,
verificadores espaciais, traduções e compilação das extensões foram conferidos.
Documentação do produto acompanha o contrato nos dois idiomas; resultados
reproduzíveis e evidências estão no diretório externo da tarefa.

Qualificação física separada e pendente. Não houve nova captura, movimento,
alteração de configuração real, promoção de modelos, benchmark ou certificação
de ganho nesta continuação. Uso físico exige qualificação de chegada e retorno
vinculada à identidade da fonte, composição, artefato, geometria e revisão exatos.
O aceite de software não encerra C4 nem comprova cobertura física ou precisão do
equipamento. Nenhum commit, push ou deploy nesta etapa.

## Disponibilidade espacial de eventos

O caminho de mapeamento, tracking, agrupamento e notificações preserva motivos de indisponibilidade por objeto e distingue grupos parcialmente localizados. Uma posição anterior não substitui uma observação atual sem mapeamento. Encerramentos conservam a última evidência do sujeito; recuperação informa trajetória parcial e não liga pontos através da lacuna registrada. O diagnóstico do nó e a notificação exibem degradação espacial. Esse contrato de software não certifica precisão física nem reconstrói o histórico automaticamente.

## Interface de mapas e projeções — 13/10/2026

O mapeamento oferece alternância entre panorâmica e composição em telas estreitas, mantendo as duas vistas montadas e seus estados de navegação. No seletor, referências configuradas são pré-condições; fontes e cobertura continuam verificadas ao abrir a projeção. Falhas panorâmicas identificam a fonte afetada e dão acesso ao seu mapeamento, preservando as camadas disponíveis. Carregamento e impossibilidade de consultar o estado não são tratados como reprovação geométrica.

Validação de interface: alternância móvel e abertura contextual observadas no navegador, com regressões de foco, traduções e ciclo de vida aprovadas. A inspeção da composição real exibiu uma panorâmica salva e identificou geometria indisponível de outra fonte sem bloquear a cena. Não foram executados movimentos, nova captura ou ativação de calibração. Essa entrega não certifica precisão física, cobertura ou qualificação métrica.

### Região visual no pipeline e prévia de áreas — 14/10/2026

Implementado o operador `camera.panorama_region`, com desenho sobre referência
salva esférica ou de imagem única, critérios espaciais, correspondência visual e
saída de observações para `camera.region_occurrence`. O filtro por área continua
selecionando áreas da composição por identificador e oferece prévia observacional.
O editor reaproveita a superfície de máscara de movimento; não há outro editor
de pipelines. Referência substituída não move silenciosamente o desenho salvo.

Enquadramento opcional usa o controlador de atenção existente, condicionado a
perfil qualificado para a mesma referência e confirmação sobre tracking nativo.
Execução remota permanece bloqueada sem qualificação dos artefatos no executor.
Validação automatizada e de navegador usa imagens sintéticas e serviços simulados.
Não houve movimento físico nem habilitação de pipelines reais nesta entrega;
precisão física, iluminação noturna e retorno mecânico continuam pendentes de
aceite por câmera. Esta implementação não certifica cobertura ou segurança física.
