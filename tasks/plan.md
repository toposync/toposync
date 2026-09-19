# Plano: destravar a captura panorâmica existente

Estado: planejado, sem implementação ou ensaio físico nesta etapa. Ponytail full.

## Objetivo e aceite

Resolver os dois bloqueios identificados na [investigação de 13/09](../docs/panoramica-investigacao-validacao-20260913.md): encerramento após pré-condição alterada sem comando emitido e avanço até perder uma referência navegável.

Aceite positivo: percorrer um trecho bidimensional, executar e comprovar o retorno entre faixas, guardar fotografias, montar resultado aproveitável e preservar o recorte do usuário. Aceite de falha: evidência insuficiente produz parada confirmada, fotografias preservadas e motivo correto, sem repetição incerta nem alegação de cobertura completa. Uma parada segura sozinha não encerra o objetivo positivo.

## Decisões mínimas

- Alterar o fluxo existente em `panorama_scan.py` e o diagnóstico do adaptador em `panorama_capture.py`. Reutilizar observação de estabilidade, verificação de referência, rota visual, orçamento, persistência e recuperação existentes. Conferir todos os chamadores dos pontos alterados antes de implementar.
- Manter o limite de 1 px e os critérios de chegada. A recusa de 1,45 px não prova ruído: faltou o par original. Recuperar por nova evidência, sem aceitar automaticamente o quadro recusado como nova referência.
- Uma fotografia pode ser aproveitável sem servir para navegação. Guardá-la não deve substituir a última referência confiável. Reutilizar os identificadores e conexões já persistidos; acrescentar somente o estado indispensável. Estados antigos sem essa prova exigem relocalização antes de novos movimentos.
- Separar emissão ausente de emissão incerta na origem do erro. Não inferir isso apenas de um código genérico: `correction_precondition_changed` aparece em mais de uma etapa.
- Uma reobservação por movimento lógico, dentro do orçamento original; cancelamento, perda de controle e troca de identidade da captura impedem repetição. Não reiniciar o contador ao retomar o trabalho.
- Na perda de suporte visual, tentar reconhecer a referência com a câmera parada. Se reconhecida, usar apenas a rota existente cuja conexão esteja comprovada, ou destino de retorno já validado. Continuar a direção pendente somente após chegada confirmada. Se nenhuma opção existir, interromper aquela exploração e preservar o resultado parcial.
- Pouca textura não comprova limite mecânico nem câmera parada. Encerrar tentativas sem evidência com Stop e resultado inconclusivo; não reduzir indiscriminadamente o tempo de observação de todas as câmeras.

Fluxo: **recusa antes do comando → reobservação única → referência válida → comando**; ou **referência insuficiente → recuperação verificável → direção pendente**; ou **sem recuperação verificável → parada e resultado parcial**.

## Execução

Lista operacional e critérios por tarefa em [todo.md](todo.md).

1. Corrigir a recuperação antes da emissão e guardar o par recusado.
2. Proteger a referência de navegação e limitar exploração sem suporte visual.
3. Registrar as causas de troca de transporte e indisponibilidade de retorno.
4. Validar a sequência integrada offline; depois, realizar um único ensaio físico delimitado.

Dependências: **1 → 2 → checkpoint offline; 3 → 4; 1 + 2 + 3 → 4**. Trabalho sequencial: as mudanças compartilham a mesma máquina de estados; delegação não reduz o risco nesta correção curta.

## Verificação e orçamento

- Primeiro, testes focados por tarefa; depois, uma execução conjunta dos testes de varredura, captura e API afetada. Reutilizar pytest e as câmeras simuladas do projeto. Simular o adaptador físico, sem substituir a lógica de recuperação que se pretende testar.
- Usar as imagens locais da investigação como reprodução complementar; os testes permanentes devem funcionar sem arquivos privados, credenciais ou câmeras reais. JPEG persistido não é reprodução exata do quadro original.
- Medir tempo por etapa, quantidade de comandos emitidos e quantidade de reobservações. Para suporte visual insuficiente no pulso mínimo, o aceite é não iniciar sucessivas explorações da mesma direção consumindo novamente a janela inteira de aproximadamente 13 s. Uma tentativa isolada pode ainda precisar do limite atual.
- Depois do checkpoint offline, ensaio somente na Frente, com o fluxo real, condição inicial visualmente reconhecível e transporte registrado. Delimitar duas faixas curtas no ensaio existente; se o caminho atual não permite essa delimitação sem trocar a lógica sob teste, registrar esse impedimento antes de iniciar uma captura extensa.
- Teto do ensaio: seis minutos de captura, até 24 fotografias e a reserva de parada/retorno já prevista no executor. Ao atingir o teto, preservar evidências e parar; não abrir uma segunda tentativa automática. O teto limita custo, não é promessa de duração nem substitui os limites físicos existentes.
- Provar nos registros a entrada no retorno entre faixas, sua chegada confirmada e o início da segunda faixa. Inspecionar a montagem e o fluxo de apresentação/recorte. Registrar tempo de captura e montagem separadamente. Um trecho bidimensional validado não prova alcance total da câmera nem todas as marcas.

## Interface, compatibilidade e escopo excluído

Reutilizar a interface e os estados existentes. Verificar que resultado parcial, necessidade de recuperação e sucesso sejam distintos, que as fotografias permaneçam acessíveis e que só o usuário determine o recorte. Se uma mensagem existente descreve incorretamente um novo desfecho, corrigir apenas seu mapeamento/texto nas traduções suportadas, em tarefa pontual adicional registrada antes da edição.

Ficam fora: atlas, redesenho de wizard/mapeamento, calibração de precisão, novo algoritmo de montagem, dependências novas, ajustes por marca, troca para outra resolução e captura de todas as câmeras. Manter uma transmissão por sessão para observação e fotografias; apenas tornar explícito o fallback já existente.

## Riscos, limites e rollback

- Uma parede pode tornar impossível a navegação visual. Nenhum algoritmo pode garantir retorno por imagem sem referências: o plano protege a última referência e termina honestamente se ela for perdida. Não usar duração de pulso ou pan nativo como prova geométrica de chegada.
- A correção recente do retorno permanece sem comprovação física. Testes devem alcançar essa etapa; falhar antes dela não conta como sua validação.
- Transporte direto e falta de preset têm causa específica ainda desconhecida. Registrar códigos sanitizados; não introduzir estratégias novas com base nessa lacuna.
- Preservar os muitos arquivos já modificados no projeto. Manter o delta desta correção identificável; se houver regressão, reverter somente esse delta, sem remover fotografias, artefatos, configuração ou trabalho anterior.

Não há decisão de produto pendente para iniciar as tarefas. Nenhuma implementação ou movimentação faz parte deste pedido de plano.
