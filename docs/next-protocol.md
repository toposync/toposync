# Próxima fase — qualificar o estado após a falha da Garagem

O lote de aceitação física v4 terminou pela stop rule. Não iniciar uma segunda
Garagem nem a Frente com a autorização consumida nesta fase. O trabalho
`7a3b937557e249d191435683e03b9afb` e os arquivos em
`.toposync-data/runtime/cameras/source-panorama/jobs/7a3b937557e249d191435683e03b9afb/`
são a fonte factual; ler `docs/checkpoint.md` e
`ignore/acceptance-coverage-20260915-1156-camera_3_177980/preflight.md`.

Primeira tarefa, somente leitura: estabelecer se a Garagem está parada e onde
está apontando por observação ao vivo e pelo procedimento operacional existente.
Distinguir Stop aceito, parada observada, posição ONVIF lida, retorno da
referência e prontidão do controlador. `motion_state=unknown` e
`geometry_safe=false` permanecem bloqueios; não convertê-los por inferência.
Não acionar preset, pulso PTZ ou retorno enquanto comando/observação estiverem
incertos. Preservar os presets de usuário, o artefato parcial anterior e todas
as imagens/replays da falha.

Depois, examinar apenas a primeira tentativa inferior: comando tilt -0.1/0.3 s,
Stop solicitado/aceito, 102 quadros, par recusado, geração do decoder,
comparações e leitura tilt posterior. Pergunta delimitada: o observador perdeu
uma transição tardia ou o comando não produziu progresso visual qualificável
na janela? A mudança posterior de tilt lido não responde sozinha. Não alterar
reconstrução, tolerâncias ou percurso por hipótese.

Se a evidência offline não distinguir a causa, registrar a condição faltante e
parar. Um diagnóstico físico futuro exige autorização nova e gates completos de
identidade, vídeo, posse, referência e orçamento; não é uma nova aquisição
implícita. Somente após parada/localização e prontidão operacional confirmadas
pode-se propor outra aceitação, com limites e autorização próprios. A Frente
continua não alcançada nesta rodada.

Modelo recomendado: Codex local, `gpt-6-astra` com raciocínio `high`, pela
necessidade de auditar recibos de controle e imagens sem intervenção manual.
