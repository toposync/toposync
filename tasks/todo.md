# Tarefas: destravar a captura panorâmica

Plano: [plan.md](plan.md). Implementação concluída; validação integral e ensaio físico permanecem na tarefa 4.

## 1. Recuperar a pré-condição antes da emissão

**Descrição:** guardar a recusa antes da saída antecipada de `_move` e reobservar uma única vez quando o comando comprovadamente não foi emitido. Manter separada a decisão de um novo comando das falhas posteriores à emissão.

**Aceite:**
- [x] Par recusado, identidade dos quadros, medida, razão e estado de emissão ficam preservados, respeitando as cotas existentes e sem segredos.
- [x] Nova estabilidade e referência válida permitem prosseguir dentro do orçamento original; tolerância de 1 px permanece. Segunda recusa encerra essa recuperação.
- [x] Emissão incerta, captura incompatível, expiração, cancelamento e perda de controle não geram repetição. Retomada do trabalho não reinicia a tentativa consumida.

**Verificação:** acrescentar regressões que reproduzam recusa de 1,45 px com emissão ausente, recuperação válida e persistência da recusa; verificar histórico real de chamadas ao adaptador. Executar `.venv/bin/python -m pytest -q tests/test_camera_panorama_scan.py -k 'precondition or recovery'`.

**Dependências:** nenhuma. **Escopo:** pequeno, dois arquivos.

**Arquivos prováveis:** `extensions/cameras/src/toposync_ext_cameras/panorama_scan.py`; `tests/test_camera_panorama_scan.py`.

## 2. Conservar uma referência navegável na exploração

**Descrição:** usar os contratos visuais existentes para impedir que uma fotografia apoiada somente em correspondências causais esparsas substitua a referência de navegação. Ao degradar o suporte, reobservar parado e recuperar por rota comprovada antes de nova exploração.

**Aceite:**
- [x] Fotografias aproveitáveis são preservadas, mas somente referências qualificadas autorizam navegação. Após perda dessa qualificação, nenhum novo avanço exploratório é emitido antes de relocalização/retorno verificado.
- [x] Uma recuperação delimitada alcança referência válida e permite seguir a direção pendente usando a máquina de estados existente; sem rota/destino verificável, termina parcial. Pouca textura nunca marca limite mecânico confirmado.
- [x] No pulso mínimo, suporte insuficiente não causa sucessivas tentativas com orçamento renovado. Cancelamento, retomada e limites de comandos/tempo preservam Stop e não repetem movimentos ambíguos.

**Verificação:** cena com suporte apenas causal, queda progressiva de textura, perda súbita de referência e caminho positivo de retorno seguido por segunda faixa. Verificar comandos, estado persistido e tempo com relógio controlado. Executar `.venv/bin/python -m pytest -q tests/test_camera_panorama_scan.py -k 'navigation or sparse or connection_recovery or visual_band_return or minimum_pulse'`; conferir que os testes novos são selecionados.

**Dependências:** 1. **Escopo:** médio, até quatro arquivos.

**Arquivos prováveis:** `extensions/cameras/src/toposync_ext_cameras/panorama_scan.py`; `tests/test_camera_panorama_scan.py`. Somente se o estado persistido exigir ajuste: `extensions/cameras/src/toposync_ext_cameras/source_panorama.py`; `tests/test_camera_source_panorama_api.py`.

## Checkpoint: recuperação offline

- [ ] Casos negativos não emitem comandos indevidos; caso positivo alcança o retorno e a segunda faixa.
- [ ] Testes não substituem `_pulse` ou a decisão de recuperação no cenário integrado; a simulação fica no adaptador físico.
- [x] A rota existente comporta o retorno causal ao último ponto confiável sem relaxar a prova; nenhuma captura completa foi iniciada nesta etapa.

## 3. Explicar transporte e disponibilidade de retorno

**Descrição:** acrescentar motivos sanitizados aos diagnósticos já existentes, sem alterar a seleção de transmissão ou as capacidades da câmera.

**Aceite:**
- [x] A troca de `configured` para `direct` registra motivo, instante/etapa e identidade da sessão, sem credenciais nem endereço autenticado.
- [x] A indisponibilidade de retorno distingue ausência de posição utilizável, capacidade de preset, falta de vaga e falha da operação conforme a evidência disponível; desconhecido permanece explícito.
- [x] Os motivos chegam ao artefato de diagnóstico persistido; consumidores e registros antigos continuam válidos.

**Verificação:** simular abertura recusada, timeout sem quadro novo e retornos indisponíveis; conferir o diagnóstico persistido e ausência de dados sensíveis. Executar `.venv/bin/python -m pytest -q tests/test_camera_panorama_capture.py`; acrescentar teste focado na varredura se a passagem até a persistência mudar.

**Dependências:** nenhuma; executar após 2 para manter o trabalho sequencial. **Escopo:** médio, até quatro arquivos.

**Arquivos prováveis:** `extensions/cameras/src/toposync_ext_cameras/panorama_capture.py`; `tests/test_camera_panorama_capture.py`; se necessário, `extensions/cameras/src/toposync_ext_cameras/panorama_scan.py`; `tests/test_camera_panorama_scan.py`.

## 4. Provar o fluxo completo delimitado

**Descrição:** executar regressão conjunta, reproduzir os pares locais já salvos e realizar um único ensaio curto na Frente somente após os testes offline. Atualizar o relatório com o que foi efetivamente demonstrado.

**Aceite:**
- [ ] Regressões passam: `.venv/bin/python -m pytest -q tests/test_camera_panorama_scan.py tests/test_camera_panorama_capture.py tests/test_camera_source_panorama_api.py`. A reprodução privada é complementar e não vira dependência da suíte.
- [ ] No ensaio físico delimitado, fotografias de duas faixas são persistidas e há prova de retorno executado e chegada confirmada; Stop e condição final ficam registrados. Se falhar, guardar a primeira evidência e não repetir automaticamente.
- [ ] A montagem é inspecionada; apresentação distingue parcial de completo e o recorte salvo continua sob controle do usuário. Relatório separa captura, montagem, cobertura e retorno, com duração e limitações.

**Verificação:** executar os comandos acima, depois o procedimento e os limites definidos em `plan.md`. Validar apresentação/recorte com artefato persistido e leitura após recarregar. Não exigir build de frontend para alteração apenas em Python; se houver ajuste pontual de interface, executar os testes e verificação de tipos correspondentes.

**Dependências:** 1, 2 e 3; checkpoint offline aprovado. **Escopo:** pequeno; reutilizar o executor existente e atualizar `docs/panoramica-investigacao-validacao-20260913.md` com seção de validação posterior, preservando a investigação original.

## Checkpoint: entrega

- [ ] Fluxo positivo bidimensional comprovado; parada segura isolada não conta como conclusão.
- [ ] Falhas delimitadas preservam artefatos e não provocam repetição incerta.
- [ ] Evidências, tempo observado e limitações entregues. Nenhuma alegação de alcance total ou validação de todas as marcas a partir desse ensaio curto.
