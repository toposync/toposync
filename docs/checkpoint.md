# Checkpoint — aceitação física da política regional 4

## Retomada 15/09/2026, aproximadamente 14:00 local

Login normal confirmado **pela guia Chrome** em
`http://127.0.0.1:8100/settings`: “Configurações Calza” e botão “Sair”. Mesmo
backend PID 87516 continuava vivo, health 200 às 16:59:59 UTC. HEAD
`0ca8d6bc0b61ad1e746a9c59b7476c6cda087cf3` e os 127 hashes da versão
congelada conferiram; o trabalho anterior registra `initial_region` v4.
Nenhum outro trabalho ativo e nenhum novo v4 no histórico. Os testes locais
dirigidos do checkpoint anterior continuam válidos; não foram repetidos.

A aquisição Garagem `7a3b937557e249d191435683e03b9afb` já consumiu a única
tentativa desta fase. GET autenticado do mesmo trabalho: `failed`,
`motion_not_observed`, aquisição `incomplete`, reconstrução e retorno `pending`,
`physical_state=unknown`. GET físico/controle atual: pan -0.337302,
tilt -0.296875, `move_status=UNKNOWN`; controlador `state=idle`,
`motion_state=unknown`, `geometry_safe=false`. Snapshot novo da interface,
carimbo visual 14:00:12, mostra portão, parede e piso; ele indica o
enquadramento encontrado, sem certificar Stop, retorno ou prontidão do
controlador. Nenhum comando PTZ nem nova aquisição nesta retomada.

**Stop rule ainda ativa:** encerramento seguro da Garagem não comprovado.
Garagem mantém a classificação anterior; Frente permanece **não alcançada**.
Não há nova panorâmica/máscara para comparação de cobertura. O usuário precisa
decidir se autoriza uma qualificação operacional separada do estado da
Garagem, dentro dos controles existentes, antes de qualquer nova tentativa
física; este lote não a inclui. `docs/next-protocol.md` já descreve a próxima
fase necessária e não foi substituído.

---

## Atualização 15/09/2026 após login normal

Sessão autenticada “Calza” confirmada pela interface; backend local PID 87516,
porta 8100, política congelada `initial_region` v4 carregada no trabalho.
HEAD inalterado; 127 hashes conferidos antes e depois. A Garagem teve **uma**
aquisição normal, trabalho `7a3b937557e249d191435683e03b9afb`, sem ajuste
por câmera ou intervenção no percurso. A Frente teve **zero** aquisições.

A Garagem persistiu `initial-reference.png` e `capture-0000.jpg`. Na primeira
visita inferior, tilt -0.1/0.3 s, comando e Stop aceitos, mas 102 quadros não
comprovaram a transição visual antes do orçamento de observação. O primeiro
bloqueio foi `motion_not_observed`; o comando ficou `uncertain`. Aquisição
**reprovada/incompleta** para cobertura; montagem **não alcançada** por uma só
fotografia, sem panorâmica nova ou máscara. Publicação **não alcançada**;
artefato anterior `e0422198874e42beb948e1a0500d37ce` preservado.
Stop solicitado e aceito, **parada inconclusiva**; retorno **não alcançado**.
Controlador `motion_state=unknown`, `geometry_safe=false`, prontidão
**reprovada** para prosseguir com movimento. A posição tilt lida posteriormente
mudou de -0.34375 para -0.296875; isso não valida o efeito visual nem o retorno.

**Stop rule:** encerramento físico da Garagem sem confirmação visual. Nenhuma
aquisição Frente, nenhum segundo ensaio, nenhuma correção de código. O protocolo
atual termina aqui. A primeira condição faltante é qualificação operacional
independente de parada/localização após `motion_not_observed`; a causa física
da observação permanece inconclusiva. Evidências resumidas em
`ignore/acceptance-coverage-20260915-1156-camera_3_177980/preflight.md` e
originais/diagnósticos no diretório do trabalho. Próxima fase delimitada em
`docs/next-protocol.md`; exige nova autorização para qualquer movimento.

---

## Estado anterior ao login normal

15/09/2026. Resultado daquela tentativa: **não alcançado por gate operacional**. Nenhuma
aquisição criada, nenhuma câmera movimentada por esta execução e nenhuma cobertura
nova avaliada. Não há reprovação física da política nem aprovação visual.

## Revisão e estado

- HEAD: `0ca8d6bc0b61ad1e746a9c59b7476c6cda087cf3`; código local sem commit,
  incluindo a política v4 do Goal anterior. Os 127 hashes de
  `ignore/coverage-implementation-20260915/frozen-code.json` conferiram nesta
  fase. Não houve edição de código, configuração ou limites, nem staging/reversão
  de trabalho alheio.
- `docs/checkpoint.md`, exigido como primeiro arquivo de handoff, não existia.
  O checkpoint real anterior estava em `docs/panorama/checkpoint-entrega.md`.
  Não foi encontrado conflito material com o protocolo; essa divergência de
  caminho fica registrada, sem tratá-la como autorização para alterar decisões.
- Protocolo operacional: `docs/panorama/protocolo-aceitacao-fisica.md`.
  Referências estáticas: `docs/panorama/regioes-de-avaliacao.md`. Emenda do
  quadro histórico ausente continua vigente; o negativo histórico não foi
  fabricado nem reapresentado como aprovado.

## Ações e evidências

- Li o checkpoint esperado (ausente), depois o protocolo, depois o checkpoint
  real, hashes, artefatos históricos e configuração relevante. O HEAD e o amplo
  worktree sujo anterior foram preservados.
- As identidades estáticas coincidem: Garagem `camera_3_177980/profile_1` e
  Frente `camera_reolink_frente/wide_main`, ambas configuradas e habilitadas.
  Isso não confirma identidade óptica/perfil de controle **ao vivo**.
- Os trabalhos regionais persistidos consultados estão terminais; nenhum novo
  trabalho foi criado. A Garagem histórica `2cd035bb...` permanece parcial,
  com artefato `e0422198...`; a Frente v3 `af03d151...` permanece ready,
  estado físico registrado stopped. Esses estados históricos não substituem
  observação atual ou retorno confirmado.
- Espaço local medido: 226 GiB disponíveis; suficiente para a reserva de 2 GiB
  por trabalho. Ainda falta a qualificação de reserva feita pelo serviço.
- Havia apenas frontend antigo (PID 76692, iniciado em 13/09); não servia como
  prova da política v4. Uma inicialização normal do backend em sandbox terminou
  por `PermissionError` no bind local/MediaMTX, sem abrir porta. A mesma
  inicialização, sem bypass e sem mudança de configuração, foi feita com acesso
  local permitido. Backend PID **19747**, iniciado 15/09 09:55:59 local,
  cwd `/Users/c/Projects/toposync-2`, comando
  `.venv/bin/python -m toposync serve --host 127.0.0.1 --port 8100 --data-dir .toposync-data --log-level warning`.
  Porta 8100 em escuta, GET `/api/health` = 200/status ok. PID e sessão são
  observações deste checkpoint; revalidar no próximo turno.
- A instância usa autenticação normal (`TOPOSYNC_AUTH_MODE` não definido;
  default enforced em `src/toposync/runtime/auth.py`). Chrome e navegador
  interno mostraram login vazio, sem sessão existente. GET anônimo da fonte
  Garagem = **401 application/json**. O usuário foi solicitado a entrar na
  instância local pelo Chrome, sem compartilhar senha na conversa; ainda não há
  confirmação de login neste checkpoint. A guia Chrome foi preservada para o
  handoff.

Evidências locais: `ignore/coverage-acceptance-20260915/health-response`,
`source-anonymous-response.json`, `backend.log` (falha de sandbox) e
`backend-escalated.log` (instância atual). Os arquivos históricos permanecem
em `.toposync-data/runtime/cameras/source-panorama/` e nos diretórios `ignore/`
referenciados no protocolo. Não imprimir credenciais dos logs.

## Validações e pendências

| Item | Classificação | Motivo |
|---|---|---|
| Integridade do código congelado | aprovado localmente | 127 hashes sem divergência |
| Testes locais do Goal anterior | aprovado localmente | 70 direcionados + typecheck, sem evidência física |
| Backend normal e saúde local | aprovado nesta observação | PID/porta/health verificados |
| Autenticação para pré-verificação | reprovado no acesso anônimo; não alcançado no acesso autenticado | GET da fonte = 401, login pendente |
| Identidade óptica, vídeo, referência, posse, Stop/retorno ao vivo | não alcançado | requer acesso autenticado antes de qualquer movimento |
| Garagem: aquisição, cobertura, reconstrução, publicação, encerramento | não alcançado | zero jobs novos |
| Frente: aquisição, cobertura, reconstrução, publicação, encerramento | não alcançado | sequência não iniciada |
| Comportamento físico da v4 | inconclusivo | nenhum ensaio físico executado |

**Stop rule aplicada:** sem pré-verificação autenticada, não criar aquisição nem
suprir o gate com bypass, token extraído de banco, credenciais alteradas ou
imagem histórica. Não há causa do produto demonstrada para corrigir.

Próximo passo delimitado em `docs/next-protocol.md`: revalidar PID/versão e
sessão autenticada; somente então continuar a pré-verificação e, se todos os
gates passarem, uma aquisição na Garagem e uma na Frente. Falha de segurança
compartilhada interrompe o lote. Não repetir ensaio nem iniciar segundo lote.
