# Investigação da validação de panorâmicas — 13/09/2026

Escopo: análise do código e das evidências já guardadas. Nenhum movimento, chamada à câmera, reinício ou alteração no produto nesta investigação. A comparação offline usou o ambiente `.venv` do projeto.

## Conclusão

Os dois ensaios isolados falharam durante a primeira faixa horizontal. Nenhum chegou a executar `visual_band_return_correction`. Portanto, não validam nem refutam fisicamente a última correção de retorno entre faixas.

Existem dois bloqueios distintos: uma pré-condição visual sem recuperação quando o comando ainda não foi enviado; e perda de referências navegáveis ao entrar em uma região com pouca textura. A aceitação de uma fotografia por evidência de movimento não garante que ela sirva como referência para navegação e retorno.

## Evidências

Diretório dos ensaios: `.toposync-validation-20260913/runtime/cameras/source-panorama/`. Cada trabalho possui `job.json`, `scan-manifest.json` e `scan-diagnostics.json` em `jobs/<identificador>/`.

| Trabalho | Fotografias | Tempo ativo | Bloqueio |
| --- | ---: | ---: | --- |
| `45be39e5f8c44e8fab65c67d5b8242e9` | 17 | 104,82 s | `correction_precondition_changed` antes de emitir o próximo comando horizontal |
| `fe18ba59c3984c10b114d58761fba045` | 11 | 103,54 s | conexão visual recusada; referências de recuperação insuficientes |

São resultados parciais. O primeiro gerou o artefato `32ab2f6ead674fd3aa9f47abe0ec69c4`, com qualidade de montagem aprovada e cobertura parcial. O segundo gerou `9d86899c5a78491bae46c947201e34fa`, com qualidade de montagem não aprovada. Montagem aprovada não prova cobertura completa nem retorno físico.

### Primeiro bloqueio: pré-condição

Em `panorama_scan.py`, `_move` compara o quadro esperado com uma nova observação antes do comando. O deslocamento calculado foi **1,449 px**, acima do limite de **1,0 px**. O diagnóstico registra `command_outcome=not_issued`; a tentativa terminou em aproximadamente 0,059 s.

Essa recusa ocorreu antes da verificação de idade do quadro. Não há evidência para atribuí-la à latência. A saída também antecede a preservação normal dos quadros para reprodução: o par exato dessa recusa não foi guardado. Assim, permanece indeterminado se houve deslocamento real residual ou variação da estimação visual.

`_recover` não contempla `correction_precondition_changed`. Uma guarda correta para impedir um comando com premissas alteradas transforma-se, portanto, em encerramento da varredura sem reobservação delimitada.

### Segundo bloqueio: observabilidade

Os pares rejeitados guardados mostram quase exclusivamente uma parede próxima, desfocada e com pouca textura. A execução aceitou transições com suporte esparso vinculado ao comando, mas recusou a conexão geral e as referências necessárias ao retorno.

No último movimento, movimento e parada foram observados; a comparação final retornou `insufficient_correspondences`. As tentativas de reconhecer a última fotografia e a referência original obtiveram, respectivamente, melhores candidatos com 19 e 15 correspondências internas ao modelo, insuficientes para os contratos de referência. Não havia destino de retorno por posição ou preset guardado.

A reprodução offline de `capture-0010.jpg` contra `rejected-pair-910cc9af3e6e4f868996f3125d09f3dc-after.jpg` confirmou a distinção: `_command_match` aceita suporte esparso; `_match`, `_anchor_match` e `_no_effect_match` recusam. São imagens JPEG persistidas, não reprodução bit a bit dos quadros originais; a contagem offline de 23 correspondências não substitui a contagem do diagnóstico original.

O pulso já estava no mínimo de 0,12 s: subdividi-lo não oferecia nova tentativa. O retorno visual entre faixas pertence ao caminho de faixa concluída; esta execução perdeu a conexão antes de chegar a esse caminho.

Quatro das doze tentativas de movimento consumiram aproximadamente 13 s cada: **52,77 s, cerca de 51% do tempo ativo**. A confirmação demorou em uma cena pouco observável apesar dos pulsos curtos. Isso não é evidência de que o motor precisasse desse tempo.

## Condições de validação e limites

- A configuração ONVIF, controle e origem da transmissão coincide entre a instância principal e a isolada. As únicas diferenças encontradas em `sources` são identificadores e revisão dos artefatos de panorama.
- O trabalho anterior da principal `68148b0f9532427eb5a92fc34a4754c6` registrou transporte `configured`; os isolados registraram `direct`. O adaptador pode abrir diretamente a transmissão ONVIF após falha da origem configurada, mas o motivo específico dessa troca não ficou preservado. Não é possível atribuir causalidade à troca.
- Todas as fotografias examinadas têm 3840 × 2160. Não houve demonstração de captura em baixa resolução nesses ensaios.
- A primeira fotografia da segunda tentativa registra pan nativo 123, próximo do pan 95 deixado pela primeira. Ela começou perto do extremo, não na mesma referência inicial. Esses valores são unidades nativas, não graus.
- A indisponibilidade de destinos de retorno já aparece nos trabalhos anteriores da principal `68148b0f9532427eb5a92fc34a4754c6` e `ddf3ba375c8147039831bb4fb1dcbe07`. Não surgiu apenas pela cópia de configuração. A causa específica da indisponibilidade continua sem detalhe suficiente.
- Os testes anteriores do retorno entre faixas partem de uma rota construída e imagens sintéticas com textura, substituindo a execução física dos pulsos. Não cobrem estes dois bloqueios anteriores à entrada no retorno. Não foram reexecutados nesta investigação sem mudanças no produto.

## Correções candidatas e prova exigida

1. **Pré-condição alterada sem comando emitido:** preservar o par recusado e a razão exata; permitir reobservação limitada somente quando a não emissão estiver comprovada. Exigir nova estabilidade, referência visual, identidade da transmissão e posse do controle válidas, dentro do orçamento original. Não aumentar o limite de 1 px sem evidência, nem repetir cegamente movimentos com emissão incerta.
2. **Referência navegável separada da fotografia aproveitável:** acompanhar a última referência de navegação confiável e a qualidade das conexões seguintes. Quando o suporte degrada, retornar de forma delimitada enquanto ainda existe referência verificável; se ela já foi perdida, parar e declarar recuperação necessária. Não promover suporte esparso de um comando a geometria global nem confundir região não observável com limite mecânico.
3. **Observação com falha delimitada:** discriminar pouca textura, ausência de movimento e vídeo atrasado. Evitar repetir integralmente a janela de observação para cenas que já demonstraram não oferecer evidência suficiente, preservando Stop, limites e resultado inconclusivo honesto.
4. **Diagnóstico e ensaio reproduzíveis:** registrar motivo da troca de transporte, motivo de indisponibilidade do retorno, quadro esperado e quadro recebido na recusa. Registrar a condição inicial; uma tentativa iniciada no ponto final da anterior deve ser identificada como tal.

Antes de outra panorâmica completa, testar offline: pré-condição alterada com emissão comprovadamente ausente; emissão incerta sem repetição automática; perda de textura com suporte apenas causal; pulso mínimo sem repetição infinita; cancelamento e orçamento durante recuperação. Testes devem verificar comandos emitidos e transições de estado, além de códigos de erro.

Depois das correções, um ensaio físico curto deve atingir deliberadamente o retorno entre faixas e guardar suas evidências. Esse é o aceite que faltou na validação anterior. A reprodução offline delimita os defeitos; não substitui essa prova física.

## Implementação posterior

As correções planejadas foram implementadas em 13/09/2026, sem chamada ou movimento de câmera:

- uma pré-condição recusada antes da emissão preserva o par, a identidade e a medida inicial, e permite uma única reobservação estável dentro do orçamento original;
- a tentativa consumida fica persistida até que o novo movimento tenha uma intenção durável, impedindo sua repetição após reinício;
- suporte `sparse_distributed_command_transition` continua válido apenas para provar o comando e seu retorno, sem virar referência de navegação;
- ao encontrar esse suporte, a câmera retorna ao último ponto distribuído verificável, registra a direção como não confirmada e pode seguir para outra região pendente; a ausência de prova termina parcial;
- o fallback de transmissão registra o motivo sanitizado, e indisponibilidade por capacidade de preset passou a ser distinta de ausência geral do recurso.

Validação rápida concluída: 48 testes focados em recuperação, conexão, pré-condição e transporte; 329 testes dos módulos de captura e API; 18 testes do retorno entre faixas; 61 testes de retomada; compilação Python, Ruff e verificação de whitespace. Algumas seleções de testes se sobrepõem. Não foram executadas a suíte completa de `test_camera_panorama_scan.py`, captura física, montagem real, inspeção da interface ou persistência real do recorte. Esses itens permanecem no passo 4 de `tasks/todo.md`.
