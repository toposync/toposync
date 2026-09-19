# Aceitação física de cobertura — política regional 4

**Protocolo para execução posterior. Nenhuma câmera acessada nesta etapa local.**
Uma aquisição na Garagem, depois uma na Frente, sem repetição, com uma única
versão congelada. Os testes locais não aprovam a cobertura física.

## Preparação e critérios de entrada

1. Conferir `ignore/coverage-implementation-20260915/frozen-code.json`: revisão,
   hashes e alterações locais. Comparar os hashes antes de iniciar o processo e
   depois do lote. Carregar essa versão por inicialização normal do Toposync,
   somente sem trabalhos ativos. Registrar comando de inicialização, identificador
   e horário do processo; não reiniciar no meio do lote. Não incluir trabalho
   alheio em commit. Não usar um processo anterior apenas porque responde à API.
2. Conferir a configuração existente e a identidade resolvida no produto:
   Garagem `camera_3_177980` / `profile_1`; Frente `camera_reolink_frente` /
   `wide_main`. São identificadores históricos a confirmar, não parâmetros de
   percurso. Registrar lente, transmissão, resolução, nó/configuração/perfil de
   controle, capacidades e espaços de coordenadas. Se divergirem, não executar
   com identidade presumida.
3. Inventariar trabalhos, referências de retorno, presets e vínculos de calibração.
   Preservar os registros e imagens anteriores. O token 002 já foi reutilizado:
   conferir nome, proprietário e origem atuais; o número sozinho não identifica
   uma referência. Não sobrescrever preset do usuário nem declarar resolvido um
   retorno histórico não confirmado. Pendência impeditiva deve passar pelo
   procedimento existente; este protocolo não autoriza abandono ou remoção extra.
4. Verificar trabalhos/automação concorrentes no mesmo dispositivo físico e a
   disponibilidade de posse exclusiva. A posse e o fence efetivos são adquiridos
   pelo produto. Registrar separadamente estado do controlador, movimento,
   `geometry_safe` e retorno. UNKNOWN não vira IDLE por inferência. Se a operação
   normal recusar a qualificação, preservar a recusa e parar sem contornar o gate.
5. Registrar espaço livre e aprovação da reserva existente do serviço (2 GiB por
   trabalho). Preservar configuração, credenciais e firmware. Não remover
   artefatos antigos para liberar espaço durante o lote.
6. Observar vídeo ao vivo e preservar a referência inicial encontrada. Snapshot
   auxiliar usa `fresh=true&freshness=decoder`: é observação local, não exposição
   física certificada, e não substitui Stop, janela estável, identidade/geração e
   qualificação causal do scanner. Validar Content-Type e decodificação antes de
   chamar o arquivo de fotografia. Não salvar uma resposta JSON como JPG.
7. Comparar a referência com `regioes-de-avaliacao.md`. Se o enquadramento útil
   necessário ao teste estiver ausente, não reposicionar implicitamente: registrar
   a condição faltante. Referência preparada não valida partida arbitrária.
   O produto deve salvar um novo destino de retorno associado à referência atual;
   não reutilizar vínculos invalidados. Sem destino qualificado, não iniciar pulsos.

## Limites congelados

`initial_region`, versão **4**, sem ajustes por câmera. Até 24 fotografias integrais,
288 MiB de originais, 30 comandos no ledger: até 25 de aquisição (incluindo
recuperações intermediárias) e 5 reservados ao encerramento. Até 420 s ativos:
270 s de aquisição e 150 s reservados ao encerramento. Montagem até 180 s;
fila até 30 s. Stop é operação de segurança do executor existente; não representa
uma licença para mais deslocamentos quando o ledger estiver esgotado.

Metas experimentais: extensão lateral 0,8, semente lateral 0,2 e inferior 0,2
em unidades da imagem analisada. Não são ângulos nem aceite visual. Pulsos usam
a velocidade qualificada pelo executor; duração inicial 0,3 s, adaptação existente
por observação, teto 2 s. Ausência de efeito qualificada admite aumento ×1,5,
até três ocorrências por visita; avanço confirmado consome orçamento global.
Uma chamada ao destino salvo por visita, seguida de localização visual; não
repetir recall para procurar coincidência. Os custos históricos justificam usar
o orçamento v3, mas não garantem que baste para a nova trajetória.

Preservação da primeira ligação recusada: duas matrizes exatas, até 64 MiB
somadas com cabeçalhos; metadados até 1 MiB. Excesso ou erro de escrita fica
explicitamente `unavailable`; não é uma evidência completa nem autoriza movimento.

## Operação normal, uma criação por câmera

Verificação local dos arquivos congelados, executável antes de iniciar o processo
e ao encerrar o lote (não acessa câmeras):

```sh
python3 - <<'PY'
import hashlib, json
from pathlib import Path
freeze = json.loads(Path('ignore/coverage-implementation-20260915/frozen-code.json').read_text())
changed = [name for name, digest in freeze['files'].items()
           if not Path(name).is_file() or hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest]
if changed:
    raise SystemExit('Versão divergente: ' + ', '.join(changed))
print('Hashes conferidos; isso não comprova a versão de um processo já iniciado.')
PY
```

Usar a sessão autenticada e o prefixo de ingresso já existentes; não ativar bypass
para contornar autenticação. Os exemplos abaixo pressupõem `TOPOSYNC_BASE_URL`
apontando ao mesmo processo qualificado, com prefixo de ingresso se necessário,
e autenticação já fornecida ao curl pela configuração local apropriada. Não
imprimir tokens, cookies ou credenciais. Alternativa equivalente: usar a interface
normal e preservar os mesmos pedidos/respostas na inspeção de rede.

Primeiro definir os identificadores confirmados da **Garagem** e um diretório novo:

```sh
camera_id=camera_3_177980
source_id=profile_1
receipt_dir="ignore/acceptance-coverage-$(date +%Y%m%d-%H%M%S)-${camera_id}"
mkdir "$receipt_dir"
source_url="$TOPOSYNC_BASE_URL/api/cameras/cameras/$camera_id/sources/$source_id/panorama"
curl --fail-with-body -sS "$source_url" -o "$receipt_dir/source-before.json"
curl --fail-with-body -sS -D "$receipt_dir/reference-headers.txt" \
  "$TOPOSYNC_BASE_URL/api/cameras/cameras/$camera_id/snapshot?source_id=$source_id&fresh=true&freshness=decoder" \
  -o "$receipt_dir/reference-response"
```

Inspecionar os arquivos e a imagem antes de continuar. Registrar a verificação
operacional acima em `preflight.md`. Falha não autoriza a próxima chamada.
Após os gates, criar exatamente uma aquisição pelo endpoint normal:

```sh
request_key="coverage-$(date +%Y%m%d-%H%M%S)-${camera_id}"
printf '{"operation":"capture","idempotency_key":"%s"}\n' "$request_key" > "$receipt_dir/request.json"
curl --fail-with-body -sS -H 'Content-Type: application/json' \
  --data-binary "@$receipt_dir/request.json" "$source_url/jobs" \
  -o "$receipt_dir/create.json"
```

Conferir o identificador retornado em `job.id`; salvar como `job_id`. A política
efetiva deve constar como versão 4 no trabalho/`scan-manifest.json`. A conferência
do processo congelado é prévia, pois a aquisição começa após a criação. Se for
detectada versão diferente, solicitar Stop e classificar a execução inválida;
nunca criar outra. Em resposta incerta, consultar a fonte e os registros pelo
mesmo identificador de idempotência, sem uma nova operação/chave.

```sh
curl --fail-with-body -sS "$TOPOSYNC_BASE_URL/api/cameras/panorama-jobs/$job_id" \
  -o "$receipt_dir/job-latest.json"
```

Observar o trabalho na interface e consultar esse GET até estado terminal,
preservando atualizações relevantes em arquivos distintos. Timeout de consulta
não prova término. Não enviar comandos PTZ externos, fotografias suplementares,
retomada ou segunda aquisição. O produto decide visitas, duração, conexão e fim.
Em cancelamento necessário, usar o encerramento normal, uma solicitação:

```sh
curl --fail-with-body -sS -X POST \
  "$TOPOSYNC_BASE_URL/api/cameras/panorama-jobs/$job_id/stop" \
  -o "$receipt_dir/stop-request.json"
```

Consultar o mesmo trabalho para verificar o encerramento. Stop aceito não é
parada visual confirmada. Com comando, Stop ou observação incertos, nenhum novo
movimento de recuperação é autorizado. A política preserva os arquivos e tenta
Stop; não faz retorno automático para disfarçar incerteza. Retorno pendente fica
pendente, sem acionamento manual adicional neste protocolo.

Somente após encerramento seguro da Garagem, repetir o procedimento uma única
vez com `camera_id=camera_reolink_frente` e `source_id=wide_main`, diretório/chave
novos e o mesmo código. Uma falha geométrica específica encerrada com segurança
pode permitir a Frente. Falha compartilhada de posse, orçamento ou segurança
interrompe o lote. Não corrigir código, ampliar orçamento ou repetir a Garagem.

## Evidência de saída e aceite visual

Preservar `source-after.json`, resposta final do trabalho e estado do controlador.
Diretórios reais: `.toposync-data/runtime/cameras/source-panorama/jobs/<job_id>/`
e `artifacts/<artifact_id>/`. Conservar `job.json`, `scan-manifest.json`,
`scan-diagnostics.json`, referência, todos os originais, retornos, primeiro par
recusado quando existir e os arquivos de reconstrução. Manter replays disponíveis;
não inferir trajetórias alternativas a partir deles.

Abrir o artefato na interface, inclusive se candidato. Usar as URLs `image_url`
e `coverage_url` retornadas pelo produto para baixar e mostrar o integral e sua
máscara; comparar hashes com os arquivos persistidos. Conferir modelo/geométricas,
identificadores das fotografias utilizadas, versão do reconstrutor, poses com
origem/unidades e associação de comandos. Dados ausentes continuam ausentes.
Não confundir orientação reconstruída, posição lida e comando solicitado.

Avaliar, com os originais e as referências estáticas de `regioes-de-avaliacao.md`:

- Centro preservado: região útil da referência ainda representada no integral.
- Esquerda e direita: conteúdo novo demonstrável além de ambas as bordas, sem
  deslocar a janela e perder o outro lado. Frente: localizar continuação da rua
  da referência assistida. Garagem: discriminar parede/piso já visíveis de acréscimo.
- Inferior: extensão nova abaixo, preservando o chão aprovado na Frente. Piso
  que já estava na referência não conta como expansão. Não inventar áreas ocultas.
- Coerência: conexões sustentadas nos originais, inclinação global, duplicações,
  rupturas e regiões perdidas. Registrar defeitos sem recortar o resultado.

Apresentar comparação visual com referência e integral. Reportar vistas úteis e
apoios por visita/região (a v4 não é uma serpentina), durações de aquisição e
montagem, comandos de aquisição/recuperação/encerramento e saldos. Separar:
**cobertura**, **montagem**, **publicação**, **parada**, **retorno**, **prontidão do
controlador**. `route_complete`, `completed` e `ready` não significam suficiente.
Uma imagem parcial pode ser publicada como parcial. Com ativo anterior, o novo
resultado fica candidato e não substitui silenciosamente geometria de calibração.

Se o usuário escolher recorte, salvar pela interface, reabrir e conferir revisão
do recorte separada e hashes do integral/máscara/modelo inalterados. Não recortar
para avaliar ou esconder falhas. Não promover candidato para uso geométrico como
parte implícita deste lote.

Não declarar retorno confirmado sem a evidência visual do produto e seus registros;
estado IDLE isolado não resolve retorno, e retorno visual não prova prontidão
geométrica do controlador. A v4 não retoma automaticamente uma rota interrompida:
preserva contadores e artefatos, sem repetir comandos históricos.

Encerrar com os dois resultados ou o primeiro bloqueio, evidência decisiva e
condição faltante. Não redesenhar nem iniciar segundo lote. O gate desta etapa
local termina antes de qualquer execução deste protocolo.
