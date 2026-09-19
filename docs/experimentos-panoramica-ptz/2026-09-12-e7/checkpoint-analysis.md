# E7 — checkpoint após retorno não confirmado

O ensaio mede somente `ContinuousMove` ONVIF, na fonte configurada da Garagem,
com velocidade normalizada de 0,5 e duração solicitada de 1,0 s. O roteiro
esperou 1,35 s depois do aceite, sem enviar `Stop` antecipado. Nesse intervalo,
o watchdog com lease/fence do controlador permanece responsável pelo `Stop` e,
quando a câmera publica a capacidade, o `Timeout` ONVIF é uma segunda barreira.

| Direção | Resposta visual após o pulso | Retorno visual ao preset | Resultado |
| --- | ---: | ---: | --- |
| Pan positivo | 349,395 px | 0,166 px | resposta e retorno aceitos |
| Pan negativo | 389,912 px | 30,813 px | resposta aceita; retorno recusado |

O sinal óptico do pan mudou de sinal entre os sentidos: aproximadamente
`x=-303,0; y=32,8` no positivo e `x=325,2; y=29,3` no negativo. Isto demonstra
resposta observável e uma componente vertical acoplada, mas ainda não permite
tratar a coordenada ONVIF como posição geométrica precisa.

As duas janelas pós-pulso ficaram quietas. Depois do retorno negativo, a janela
também ficou quieta, porém a correspondência com a referência ficou 30,813 px
distante. Portanto a câmera não aparenta continuar em movimento; ela está numa
vista estável cuja equivalência à referência foi recusada. As três consultas de
status devolveram `UNKNOWN` com o token bruto `0`. Essa telemetria não prova a
posição nem substitui a rejeição visual. A especificação ONVIF descreve
`Error` como condição atual de erro; portanto o núcleo genérico não deve
interpretar globalmente o token não vazio `0` como ausência de erro. Uma
eventual normalização precisa ser uma regra de adaptador, sustentada por
evidência da câmera e sem alterar a política visual.

O preset temporário desse ensaio foi preservado. Nenhum `GoToPreset` adicional,
movimento, `Stop` ou exclusão de preset pode ocorrer automaticamente a partir
deste checkpoint. Isso evita transformar uma incerteza de retorno em uma cadeia
de comandos não atribuíveis.

As hipóteses ainda compatíveis são repetibilidade limitada do preset,
histerese/backlash dependente do sentido, chegada óptica mais lenta que a janela
usada ou erro de estado informado pela câmera. O resultado não discrimina entre
elas. Ele também não caracteriza tilt, variância por repetição, tempo até o
primeiro movimento visível, `AbsoluteMove`, `RelativeMove` ou precisão de
correção.

O próximo ensaio precisa ter uma hipótese de recuperação própria: confirmar
visualmente o preset preservado por procedimento deliberado e limitado, ou
assumir a vista quieta atual como nova referência por decisão explícita. Só
depois é seguro declarar um novo lote para os sentidos de tilt.

## Continuação por âncora observada

Uma âncora SIFT compacta, sem raster, foi criada a partir da vista quieta atual
e validada numa nova janela com 427 inliers, sobreposição de 0,9968 e
deslocamento de 0,123 px. Ela é uma referência observada nova; não afirma que
o preset anterior foi restaurado.

O pulso de tilt positivo, com a mesma velocidade e duração, deslocou 201,214
px, sobretudo no eixo vertical. A volta por um preset criado nessa nova
referência estabilizou 53,152 px distante dela. O comparador direto ainda
encontrou 283 inliers e 0,9067 de sobreposição, logo a diferença supera o
limiar de retorno sem depender de um simples timeout. A revalidação posterior
com o descritor compacto recusou a correspondência por suporte espacial
insuficiente; ela não transforma uma recusa em aceitação.

E7 fica concluído com uma limitação declarada. Nesta câmera e nesse perfil,
os pulsos finitos são observáveis para pan positivo, pan negativo e tilt
positivo em velocidade 0,5 por 1,0 s. Um único `GotoPreset` não é um contrato
de retorno depois de pan negativo ou tilt positivo. Qualquer continuação física
precisa qualificar recuperação óptica em malha fechada e manter o identificador
do preset e a âncora de referência com retenção segura.

## Recuperações posteriores e limites medidos

E7R criou uma referência nova em uma janela quieta. Um pulso de pan a 0,1 por
0,08 s mudou 2,392 px; seu preset novo voltou a 0,607 px, por isso o corretor
fino corretamente não foi acionado. Esse resultado só demonstra retorno exato
nesse deslocamento pequeno.

E7R2 mudou apenas a duração para 1,0 s na mesma velocidade. A câmera ficou
quieta, mas a referência apareceu a 93,841 px (sobreposição 0,9009), acima do
envelope de 35 px para o corretor fino. Nenhum micropulso adicional foi
enviado. Um E7R3 novo e independente chamou uma vez o preset preservado; a
âncora visual confirmou a volta em 0,773 px, com sobreposição 0,9963, e o
preset foi removido. Portanto o percurso recém-criado de pan positivo tem uma
recuperação exata demonstrada, mas isso não revoga os retornos recusados de
30,813 px e 53,153 px nos ensaios anteriores nem qualifica correção fina.

O E7R2 também revelou uma exigência operacional: um preset retido precisa ter
o token opaco guardado em um destino privado `0600`; o relatório público só
registra o estado e as métricas. A recuperação E7R3 localizou o único preset
por leitura, verificou a volta e apagou tanto o preset quanto esse destino
privado após a confirmação.

## Verificação no produto

O fluxo de restauração do Toposync valida a imagem retornada com sobreposição
mínima de 0,85 e deslocamento máximo de 3 px. Se o retorno não atender a esse
gate, ele termina em `return_framing_unconfirmed`, mantém a câmera parada e não
repete o `GotoPreset`. Os cenários de retorno falho, piloto não fechado e
correção interrompida foram exercitados nos testes de `panorama_scan`; portanto
essa limitação impede a progressão da varredura física, em vez de ser apenas
uma observação no relatório.

## E7R4 e E7R5 — corretor fino rejeitado, recuperação por preset verificada

E7R4 deliberadamente escolheu um pulso intermediário de pan positivo (`0,1` por
`0,35 s`) para entrar no envelope de correção: a observação após o pulso ficou a
26,699 px da referência nova, com sobreposição 0,9706. O `correct_reference` de
produção emitiu **um só** micropulso de pan positivo de `0,1` por `0,08 s`.
A observação imediatamente anterior estava a 25,986 px; a seguinte foi para
59,715 px, e a janela final ficou a 64,567 px. Ambas estavam quietas e a
correspondência continuou distribuída, portanto a rejeição foi causada por
regressão visual, não por ausência de imagem ou timeout.

O ledger marcou `visual_error_increased` e não autorizou inversão, segundo
pulso, `Stop` ou `GotoPreset` implícito. Isso é evidência positiva do fail-safe,
mas negativa para a hipótese de que esse sentido de correção fina seja
qualificado nesse deslocamento. A variação de fase mecânica, latência residual
ou não linearidade local ainda explicam o sinal; estes dados não escolhem entre
elas.

E7R5 foi um experimento novo de recuperação, não uma continuação do corretor.
Uma leitura localizou exatamente um preset recém-criado de E7R4, guardou seu
token apenas em destino `0600` e fez um único `GotoPreset`. A vista antes da
chamada estava a 60,067 px da âncora; depois estava a 0,244 px, com
sobreposição 0,9966. O preset e o destino privado foram removidos. Essa
recuperação reestabelece um ponto seguro de parada, mas só demonstra a chamada
de preset nesse percurso novo de pan positivo.

O roteirizador experimental foi corrigido para, em ensaios futuros, persistir o
token de qualquer preset preservado em destino privado `0600` antes de encerrar.
O relatório público continua sem token e sem raster. A correção trata a lacuna
operacional vista em E7R4; não altera a política de retorno do produto nem
qualifica o corretor fino.

## Hipótese causal E7R4 e correção offline do primeiro micropulso

Há uma explicação específica para a regressão E7R4 que não exige concluir que
todo retorno fino é inviável. O pulso de saída foi `pan positivo` e produziu
deslocamento visual para a esquerda; ao voltar, o corretor não possuía ainda um
modelo de resposta local e iniciou sua exploração sempre com `pan positivo`.
Esse primeiro micropulso repetiu o sentido que havia afastado a imagem da
referência. A regressão grande acionou corretamente o gate de segurança
`visual_error_increased`, que encerrou a sequência antes de testar o sentido
oposto.

O produto passou a guardar, no checkpoint do mesmo epoch de retorno, apenas a
observação causal mínima do movimento de saída: eixo, sentido, duração,
sobreposição, deslocamento e vetor de deslocamento visual. Ao começar uma
correção sem modelo medido, ele pode usar essa observação somente se: a janela
é do mesmo epoch, a correspondência foi verificada, a sobreposição é pelo menos
0,85, o deslocamento é pelo menos 2 px e a inversão em escala de um micropulso
prevê reduzir o erro presente. Então envia **uma única sonda** de 0,08 s no
sentido oposto. Ela não é tratada como ganho mecânico conhecido: só se torna
resposta reutilizável após o próximo frame fresco confirmar melhora. Semente
ausente, malformada, de outro epoch ou sem previsão de melhora preserva a sonda
legada e não reutiliza informação antiga.

Os testes offline reproduzem a geometria observada em E7R4: uma saída de pan
positivo desloca a imagem à esquerda, e a primeira sonda de retorno é pan
negativo, reduzindo o erro para menos de 3 px. Um segundo teste rejeita uma
semente de epoch antigo. A integração do scanner também foi testada para
persistir a semente somente após movimento visual verificado. Depois de uma
única resposta qualificada, o produto mantém o segundo pulso em até 0,08 s; só
libera 0,15 s depois de duas respostas observadas no mesmo epoch. Validação:
`46 passed` em `test_camera_panorama_navigation.py` e `12 passed` nos cenários
de retorno e controle de `test_camera_panorama_scan.py`. Isto é validação
offline do controlador, não validação física da Garagem nem prova para tilt.

## E7R6 e E7R6R — sentido inicial confirmado, retorno ainda recusado

E7R6 executou o mesmo percurso intermediário de pan positivo (`0,1` por `0,35
s`) com a semente causal ligada ao mesmo epoch. A saída ficou a 26,507 px da
âncora, com sobreposição 0,9699 e deslocamento visual `x=-22,923; y=-1,295`.
O primeiro micropulso emitido pelo controlador foi, como previsto, pan negativo
por 0,08 s. O ledger canônico do controlador reduziu a diferença de 26,174
para 22,555 px: há melhora física, mas não próxima do limiar de retorno. O
adaptador experimental reportou 3,830 px numa janela semelhante; E15 mostrou
depois que essa leitura pré-reduzia o frame colorido antes de chamar o
comparador, enquanto o controlador converte e normaliza o frame original.
Portanto 3,830 px não é uma medida comparável nem evidência de retorno.

Na versão que executou E7R6, a resposta qualificada liberou logo um segundo
pulso negativo de 0,15 s e ele foi visualmente inconsistente. O controlador
encerrou em estado `rejected`; a vista final quieta ficou a 15,028 px. O produto
agora exige uma segunda resposta observada antes de aumentar de 0,08 para 0,15
s. E15 eliminou o pré-processamento divergente do adaptador: ensaios futuros
entregam o frame original ao mesmo comparador do controlador. Isso corrige a
linhagem da evidência, mas ainda não prova retorno repetível.

E7R6R então chamou uma vez o preset preservado, como experimento isolado. A
âncora compacta indicou uma vista quieta a 14,977 px antes da chamada e 15,218
px depois, com sobreposição 0,9808. Essa métrica de âncora tem normalização
própria e não substitui o comparador do controlador; ainda assim não confirmou
retorno e o procedimento preservou o preset e seu destino privado sem repetir.
Assim, o experimento prova o acerto da direção inicial, mas reafirma que
`GotoPreset` não é retorno repetível neste deslocamento e que o caminho para E9
não pode assumir uma posição inicial exata após uma tentativa física.

## E15 — linhagem comum de comparação

E15 reprocessou os 16 pares adjacentes preservados da Garagem sem acesso à
câmera. O controle de uma imagem contra ela própria coincidiu nos dois caminhos.
Mas quinze pares reais divergiram quando o adaptador pré-reduzia a imagem
colorida com `INTER_AREA` antes de chamar `_match`: o controlador, por sua vez,
converte o original para cinza e aplica sua própria normalização. A ordem e o
interpolador mudavam as características SIFT e, em cenas reais, podiam escolher
outra homografia apesar de ambos os resultados aparentarem suporte distribuído.

O adaptador `bounded_motion` agora passa os frames originais diretamente a
`_match`; uma verificação de replay no primeiro par confirmou igualdade exata
de verificação, sobreposição, deslocamento e vetor de deslocamento. A cadeia de
evidência de ensaios futuros passou a ser única. E15 não requalifica E7R6 nem
permite um novo comando: apenas remove uma fonte conhecida de métricas
incomparáveis antes de um novo experimento delimitado.
