# Panorâmica PTZ: plano para a etapa atual

Autor: Mateus Calza. Data: 11 de setembro de 2026. Método: Ponytail full.

**Estado: execução aplicada, com validação física parcial.** Consulte o [relatório da execução](panoramica-estabilizacao-validacao-20260911.md). Foram corrigidas falhas de temporização, persistência e recuperação; as quatro câmeras foram ensaiadas pelo produto. Nenhuma concluiu a qualificação completa; a varredura ampla permanece condicionada ao aceite abaixo.

Este documento define a próxima sequência do [plano integrado](toposync-plano-resolucao-ponytail-revisado.md), considerando o [ensaio das quatro câmeras](/Users/c/Projects/toposync-2/ignore/panorama-actuator-diagnostic-20260911/ensaio-quatro-cameras-2026-09-11.md) e a [validação integrada anterior](panoramica-validacao-integrada-20260911.md). Os objetivos pendentes do plano integrado continuam válidos.

## 1. Objetivo imediato

Fazer o Toposync obter uma imagem utilizável, executar um movimento limitado, observar sua resposta, esperar a estabilização e confirmar o retorno pelo mesmo caminho que será usado na panorâmica. A execução deve ser automática, auditável e não depender de interpretação ou compensação do executor.

A primeira entrega é esse ciclo confiável. A seguinte é uma panorâmica com continuidade horizontal e vertical e cobertura da região útil. O desenho na planta e sua precisão métrica permanecem na etapa posterior; continuam funcionando os recursos já existentes.

Critérios invariáveis: nenhum campo geométrico obrigatório, seleção por capacidade observada, controle exclusivo do atuador, nenhuma chegada declarada apenas por resposta de protocolo e nenhuma tolerância ampliada depois de um ensaio reprovado.

## 2. Correções necessárias na leitura dos resultados

O ensaio produziu evidências úteis, mas **não isolou a causa das falhas nas Tapo nem certificou o comportamento do produto**.

| Evidência atual | Consequência para o plano |
| --- | --- |
| Os quatro streams configurados recusaram acesso com `401`; o acesso direto ONVIF funcionou no ensaio | Investigar endpoint, autenticação e seleção do transporte. `401` sozinho não identifica credencial vencida nem justifica alterar senhas |
| O ensaio chamou a API manual de controle e abriu um `FrameGrabber` direto | Movimento passou pelo Toposync, mas observação, estabilização e recuperação não foram as mesmas da panorâmica |
| Retornos usaram espera fixa e somente dois quadros; alguns pares ainda mostravam movimento | Não inferir repetibilidade do preset a partir dessa janela |
| O cálculo diagnóstico usou a translação de uma transformação afim na resolução original | Não comparar seus valores diretamente com o limite do produto: 3 pixels na largura 960, medidos por outro procedimento |
| A recuperação variou a velocidade usando a resposta do primeiro pulso em outra velocidade; alguns erros tinham suporte recusado | A oscilação pode ter contribuição do próprio experimento. Esse recuperador não deve entrar no produto |
| Frente terminou perto da referência do último ensaio | Isso não aprova os seis apontamentos do plano, a cobertura integral, nem o retorno a uma referência mais antiga |
| Corredor e Quintal pararam após o primeiro pan; Garagem após o segundo | Tilt nessas três câmeras permanece não ensaiado nesse protocolo, não reprovado |

Outra correção de direção: já existem `VisualStabilityDetector`, `VisualNavigator`, correção de retorno e fallback ONVIF em `PanoramaCamera`. Vamos corrigir e validar esses componentes, sem construir outra implementação concorrente.

## 3. Sequência de desenvolvimento e critérios de passagem

### Entrega 1 — Tornar o ensaio comparável e reproduzível

1. Preservar fotos, referências, presets retidos, configuração pertinente e versão do código. Identificar o dono e a referência de cada preset antes de qualquer limpeza. O enquadramento inicial de um ensaio não substitui a referência original de um trabalho anterior.
2. Reavaliar os pares gravados com a mesma escala e medição do produto. Manter os valores antigos como diagnóstico; criar resultado comparativo sem sobrescrevê-los.
3. Para retorno, usar a comparação distribuída já existente em `panorama_scan._match`: erro de deslocamento, sobreposição e suporte. Para apontamento, manter a medição fotográfica independente do alvo. A translação da origem da imagem não é, por si, erro do centro.
4. Fazer o próximo ensaio usar `PanoramaCamera`, o scanner e o controlador existentes. O invólucro de teste só escolhe câmeras, inicia operações e registra resultados; não decide pulsos com uma segunda fórmula.
5. Registrar a linha temporal de comando, resposta, Stop e quadros consumidos: identificador da tentativa, modo efetivo, velocidade, duração solicitada, timeout do dispositivo, instância/geração/sequência e decisão visual. Reusar diagnóstico e checkpoints existentes; sanitizar endpoints e autenticação.

**Passagem:** o mesmo material produz a mesma classificação no replay e no produto. Suporte insuficiente vira resultado inconclusivo; não autoriza correção. As sequências antigas incompletas não ganham temporalidade que não foi gravada.

### Entrega 2 — Resolver transporte e observar o quadro certo

1. Reproduzir a recusa do relay sem movimentar câmeras. Verificar a fonte exata e o vínculo câmera → canal/lente → perfil de mídia → configuração PTZ. Corrigir somente o vínculo ou autenticação cuja falha estiver demonstrada.
2. Exercitar o fallback que já existe em `PanoramaCamera._open_frames`/`frame` pelo fluxo da aplicação. Investigar por que o ensaio precisou abrir vídeo por fora desse caminho.
3. Reaproveitar o serviço de captura para os consumidores que precisam da mesma política. Compartilhar apenas onde identidade, transporte e contrato de frescor forem compatíveis; não impor fallback novo a todos os consumidores.
4. Troca de transporte, reconexão ou reinício invalida a sequência anterior e exige nova referência. Quadros em buffer não podem receber a identidade da captura nova.
5. Preservar as categorias: quadro decodificado distinto, observação visual temporal e exposição fisicamente datada. A modalidade observacional deve ser explícita; não modificar `physical_capture_verified` nem enfraquecer o endpoint de snapshot físico para fazê-lo passar.
6. Se ambos os transportes falharem, interromper antes de movimentar, com causa e ação na UI. Nenhum reinício de Home Assistant ou troca global de credenciais integra a solução por padrão.

**Passagem:** cada uma das quatro fontes abre pela aplicação ou apresenta motivo específico antes de um movimento. Reconexões e fallback não produzem aceitação de imagem antiga como atual.

### Entrega 3 — Isolar comando, atraso e estabilidade

1. Inspecionar os comandos efetivamente enviados e o estado bruto recebido: perfil, espaço de coordenadas, modo contínuo/relativo/absoluto, preset e timeout. Preservar a distinção entre capacidade anunciada, comando aceito, movimento observado e destino confirmado.
2. Medir a sequência pedido → envio → resposta → Stop enviado → Stop respondido → transição no vídeo → estabilidade. Usar relógio monotônico para durações. Tempo de resposta HTTP não é duração mecânica.
3. Auditar a programação do watchdog, atualmente feita após o retorno da execução do comando. Reproduzir com resposta lenta e verificar se ela amplia o pulso efetivo. Corrigir apenas a divergência demonstrada, mantendo o timeout de segurança do dispositivo, o Stop local e a proteção de propriedade.
4. Testar interações entre Stop do watchdog, Stop do scanner e um comando subsequente. Um Stop atrasado não pode interromper o novo dono/comando; um comando de efeito incerto não pode ser repetido automaticamente.
5. Alimentar o detector existente com quadros suficientes antes, durante e depois da ação. Tratar perda de sequência explicitamente. Só introduzir leitura sequencial limitada se o replay mostrar perda da transição no consumo da última imagem.
6. Confirmar movimento seguido de estabilidade; imagem imóvel antes da resposta do dispositivo não é chegada. Ausência de movimento usa o restante do orçamento existente e termina com motivo específico.

Preservar como ponto de partida os limites internos atuais do detector: largura 960, observação mínima de 0,5 s, janela estável de 0,8 s com pelo menos cinco quadros e timeout de 12 s. Mudanças exigem um exemplo que falha antes e passa depois, sem novos falsos positivos nas sequências de controle.

**Passagem:** sequência atrasada, imagem congelada, objeto móvel e resposta de protocolo sem movimento não passam por estabilização pós-comando. A parada encerra a ação dentro de orçamento explícito, sem interferir em outro comando válido.

### Entrega 4 — Qualificar resposta e retorno sem oscilações

1. Usar pulsos de velocidade constante na modalidade contínua e variar somente sua duração permitida. Uma velocidade diferente exige nova medição; não converter pulso curto em velocidade menor por proporcionalidade presumida.
2. Medir separadamente eixo, sentido e modalidade de comando, sempre entre imagens qualificadas. Revalidar após inversão, mudança significativa de enquadramento ou óptica; reutilizar a resposta somente no contexto em que foi observada.
3. Reaproveitar `correction_step`, `VisualNavigator` e `correct_reference`. A resposta é bidimensional: um comando de pan pode deslocar a imagem nos dois eixos. Não criar controlador escalar paralelo baseado apenas em `x`.
4. Registrar previsão e resultado de cada correção. Erro crescente, ausência de progresso acima do ruído, perda de suporte ou inversões repetidas encerram a tentativa. O sucesso de uma correção isolada não identifica uma resposta reproduzível.
5. Separar o mínimo de 50 ms imposto hoje pela aplicação da resolução física do dispositivo. Se o menor comando qualificado não puder melhorar o erro, informar precisão não comprovada em vez de oscilar ou diminuir a exigência.
6. Usar preset/posição absoluta como aproximação apenas quando compatíveis e observados. Investigar as Tapo por perfil, modo e sequência de comandos antes de atribuir a falha à marca. Adaptador específico só se houver divergência de protocolo comprovada e teste que a reproduza.
7. Referência original, orçamento de recuperação e contagem de correções são únicos por operação. Trocar estratégia não reinicia quatro correções adicionais. Sem retorno confirmado, preservar referência e preset próprio; não declarar restauração por igualdade dos valores PTZ.

**Passagem:** o ciclo move → observa → estabiliza → retorna é repetível pelo produto. Quando uma capacidade não pode ser comprovada, a câmera é limitada nessa operação, com motivo verificável. Uma recusa correta demonstra segurança, mas não conta como panorama concluído.

### Entrega 5 — Panorâmica completa da área útil e UX mínima

1. Integrar a correção no percurso existente e só então executar captura mais ampla. Validar faixa horizontal, transições entre faixas e parte inferior. Na Frente, as duas extremidades da rua; no Quintal, o chão; nas demais, seus limites úteis visíveis.
2. Certificar continuidade pelas fotos e sobreposições. Número de fotos, tamanho do mosaico ou pixels preenchidos não provam alcance mecânico. Não fechar uma faixa porque uma imagem atrasada parece repetida nem classificar falta de resposta como limite físico sem evidência.
3. O usuário continua escolhendo câmera/stream e acionando a captura. O resultado e seu recorte ficam nas configurações da fonte. Manter o wizard por ponto e as prévias bidirecionais já existentes.
4. Mostrar estados derivados do trabalho: **Verificando imagem → Conhecendo os movimentos → Capturando → Montando → Resultado**. Ações repetidas ficam bloqueadas enquanto a solicitação está pendente; **Parar** permanece disponível.
5. Mostrar miniaturas e contagem de fotos aproveitadas, com marcos discretos. Estimativa de tempo só quando sustentada pelo histórico observado; progresso indeterminado enquanto o alcance ainda está sendo descoberto. Nenhum percentual ou celebração de conclusão inventado.
6. Distinguir **imagem parcial disponível**, **captura interrompida**, **controle não confirmado** e **retorno não confirmado**. Se nenhuma foto foi salva, não mostrar “as fotografias foram guardadas”. Mensagem persistente, ação contextual e detalhes técnicos recolhidos.
7. Garantir teclado, foco previsível, anúncio moderado de estados, movimento reduzido, tradução português/inglês e rotas compatíveis com ingress. Confirmar recebimento do clique imediatamente; sucesso físico depende da evidência.

**Passagem:** o resultado útil é visível e retomável pelo Toposync; lacunas permanecem explícitas. Recortar não apaga a reprovação de uma região obrigatória. A pessoa não precisa preencher parâmetros geométricos nem interpretar códigos ONVIF.

## 4. Matemática e critérios preservados

Usar a solução local já implementada, com erro bidimensional e unidades do comando realmente observado:

```text
e_depois ≈ e_antes + J Δu
Δu = −g (JᵀJ + λI)⁻¹ Jᵀ e
```

`J` é medido com quadros qualificados; ganho, amortecimento e saturação permanecem internos. Não é um modelo global do motor, não converte segundos em graus e não certifica precisão em metros.

| Decisão | Critério desta etapa |
| --- | --- |
| Retorno | Comparação com a imagem original: deslocamento até 3 pixels na escala 960, sobreposição mínima de 85% e suporte qualificado |
| Chegada a alvo | Medição fotográfica independente do alvo até 3 pixels na escala 960; previsão de pose não substitui esse resultado |
| Resposta do motor | Movimento distinguível do ruído e temporalmente relacionado à tentativa, seguido de estabilização |
| Orçamento | Conservar os limites atuais; até quatro correções finas e limite total existente de navegação. Recuperadores não renovam orçamento |
| Generalização | Seleção por capacidades e evidências; quatro dispositivos não certificam todas as marcas, modelos ou firmwares |

## 5. Testes automatizados e validação real

Acrescentar regressões nos testes existentes, com simuladores independentes das fórmulas do controlador. Teste verde não substitui ensaio físico.

| Comportamento | Verificação mínima |
| --- | --- |
| Transporte | Relay recusa; fallback direto correto; perfil ambíguo recusado; dupla falha; troca de geração; credenciais ausentes dos diagnósticos |
| Controle | Resposta lenta, watchdog, timeout do dispositivo, Stop tardio, cancelamento, perda da concessão, idempotência e modo relativo efetivamente usado |
| Observação | Movimento atrasado, quadro repetido com sequência nova, perda de frames, baixa taxa de quadros, desfoque, vento/objetos móveis e movimento apenas numa região |
| Geometria | Mesmo par em resoluções distintas; rotação/escala com pouca translação da origem; suporte concentrado; textura repetitiva; medida não qualificada nunca habilita pulso |
| Correção | Eixos acoplados, inversão, zona sem resposta, quantização, velocidade sem proporcionalidade, resposta incoerente e mínimo de pulso que impede convergência |
| Ciclo de vida | Retorno sem confirmação preserva recursos; fallback não renova orçamento; reabrir trabalho não dispara movimento; retomada acrescenta fotos novas no mesmo trabalho |
| UI e integração | Erros antes da primeira foto, resultado parcial, parar, continuar, recorte, traduções, ingress, wizard e consumidores de mapeamento existentes |

Executar primeiro os arquivos diretamente afetados de captura, protocolo, controlador, estabilidade, scanner e navegação. Depois, a regressão integrada pertinente, TypeScript/build e os testes de navegador da panorâmica. Não somar contagens de execuções repetidas como evidência adicional.

**Protocolo físico da próxima execução:**

1. Confirmar vídeo e preservar referências das quatro câmeras sem movimento. Conferir também os presets retidos de trabalhos anteriores.
2. Ensaiar sequencialmente Frente, Corredor, Garagem e Quintal com o mesmo código. A Frente serve como comparação; as demais recebem o mesmo critério.
3. Por câmera, começar com um pequeno movimento e retorno. Somente após passar, completar pan/tilt nos dois sentidos. Para repetibilidade, prever três ciclos por sentido, interrompendo na primeira perda de confirmação. Todos os pulsos, aproximações e recuperações entram no orçamento registrado.
4. Separar sequências usadas para ajustar a lógica das usadas no aceite. Preservar tentativas recusadas e marcar direções não executadas como **não ensaiadas**.
5. Só ampliar para captura panorâmica depois da qualificação. Ao final, confirmar quadro de retorno, estabilidade, Stop e recursos próprios retidos/removidos. Deixar a aplicação disponível em 5174.

Se Corredor ou Garagem ainda estiverem deslocadas, a próxima execução começa conciliando essa pendência com suas referências. Não criar um novo “original” silenciosamente nem repetir a cadeia anterior de recuperadores.

## 6. Padrão de implementação e controle do escopo

Reusar `capture_service.py`, `processing/frame_grabber.py`, `panorama_capture.py`, `panorama_scan.py`, `processing/panorama_stability.py`, `panorama_navigation.py`, `ptz_controller.py` e o adaptador ONVIF. Ajustes visuais ficam nos componentes de panorâmica da extensão. Corrigir o ponto compartilhado e conferir seus consumidores; nenhuma exceção por identificador das quatro câmeras.

Preservar alterações paralelas do checkout, artefatos ativos, pontos da composição, revisão geométrica e comportamento legado. Commits, publicação e alterações externas não são parte deste planejamento. Nenhuma dependência, serviço de jobs, cadastro de capacidades ou interface de parâmetros nova sem necessidade demonstrada.

Ficam para depois desta estabilização: SLAM/3D, reconstrução nova do zero, calibração de toda a faixa de zoom, redesign amplo, novos protocolos preventivos e gamificação além dos marcos de progresso. Permanecem pendentes no plano integrado a precisão na planta, a disponibilidade contínua de mapeamento e os apontamentos completos. Sucesso neste ciclo não certifica esses objetivos.

## 7. Definição de encerramento desta etapa

- Protocolo de ensaio e produto usam a mesma cadeia de controle/observação e métricas comparáveis.
- Cada câmera tem resultado por capacidade: aprovado, reprovado, inconclusivo ou não ensaiado; nenhuma inferência de compatibilidade universal.
- Transporte, movimento, estabilidade, retorno e cobertura têm evidências próprias e causas de falha específicas.
- As câmeras aprovadas produzem panorâmica útil pelo Toposync. Nas demais, recusa e preservação são verificadas sem chamar isso de conclusão funcional.
- A UI informa o resultado e o próximo passo sem parâmetros técnicos obrigatórios.
- O relatório final compara antes/depois por falha, aponta a alteração que a resolveu e registra os limites ainda abertos.

**Regra para sair do ciclo de tentativas:** nova tentativa precisa testar uma hipótese diferente sustentada por replay, instrumentação ou correção concreta. Repetir a captura, aumentar a tolerância ou trocar o recuperador não constitui progresso por si só.

Skills aplicadas: [Ponytail](/Users/c/.codex/plugins/cache/ponytail/ponytail/4.9.0/skills/ponytail/SKILL.md) para reutilização e escopo; [Feedback Loop](/Users/c/.codex/plugins/cache/universal-design-principles/interaction-and-control-principles/1.0.0/skills/feedback-loop/SKILL.md) e [estados e latência](/Users/c/.codex/plugins/cache/universal-design-principles/interaction-and-control-principles/1.0.0/skills/feedback-loop-states-and-latency/SKILL.md) para visibilidade de estado e recuperação. Nenhum plugin adicional é necessário nesta etapa.
