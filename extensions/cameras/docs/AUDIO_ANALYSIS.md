# Análise local de áudio

`camera.audio_features` analisa o artefato `audio` de um pacote, preservando seu
identificador e ciclo de vida. Aceita PCM `numpy.int16`, mono ou estéreo, entre
8.000 e 96.000 amostras por segundo, até trinta segundos por pacote. O operador
não abre câmeras nem comprova que uma fonte fornece áudio.

Metadados obrigatórios do artefato:

```json
{
  "sample_rate": 16000,
  "clock": {
    "started_at": 123.0,
    "basis": "capture",
    "uncertainty_seconds": null
  }
}
```

`basis` pode ser `capture`, `receipt` ou `synthetic`. A origem declara o relógio;
o operador não transforma recepção em captura sincronizada. Incerteza ausente
permanece desconhecida. Nenhuma frequência é convertida em direção ou distância.

Para sugerir correlação entre fontes, o produtor precisa declarar `source_id`,
`capture_id` e uma `clock.synchronization_reference` comum, correspondente a uma
sincronização previamente verificada. Somente janelas acima do baseline e com
incerteza finita de captura participam. O operador compara intervalos ampliados
pela incerteza entre as últimas 32 capturas elegíveis. O resultado
`possible_overlap` é apenas um candidato temporal: não confirma o mesmo som,
não deduplica notificações e não determina posição. Relógio de recepção ou
baseline ausente deixam essa comparação indisponível.

Sem artefato, `payload.audio_capture` aceita o mesmo relógio e `availability`
igual a `no_audio` ou `stream_lost`. Essas condições são declarações do produtor,
não uma descoberta realizada pelo analisador. Sem declaração, o estado é
`audio_missing`; amostras presentes abaixo do limiar digital geram `silent`.
Metadados inválidos geram `unavailable`.

Cada janela de até um segundo fornece tempo, duração, valor eficaz normalizado,
pico, fração saturada e frequência dominante. Os canais são analisados sem soma
de fases. Um som curto pode aparecer no pico mesmo quando sua média for baixa.
As medidas não classificam fala, vento, animal, perigo ou intenção.

Anomalia exige configuração explícita `baseline` com `reference`, `rms` e
`multiplier` (padrão 3). O resultado informa apenas se o valor eficaz ultrapassa
o limiar declarado; não há confiança probabilística calibrada. Sem baseline,
a comparação fica indisponível. Valores sintéticos não qualificam uma câmera.

## Ensaio por arquivo

Em **Configurações → Câmeras → Analisar amostra de áudio**, selecione o arquivo
e use **Analisar amostra**. A comparação opcional aceita um segundo arquivo do
ambiente habitual; seu valor eficaz agregado é a referência explícita. A tela
mostra os intervalos da amostra e não inventa seu horário de captura. Não há
reprodução automática nem controle de dispositivo. Uma amostra analisada não
comprova ingestão de áudio da câmera.

`POST /api/cameras/audio/analyze` recebe WAV PCM de 16 bits no corpo da requisição,
sem acesso a caminhos do servidor. Exige parâmetros `started_at` e `clock_basis`.
São opcionais `clock_uncertainty_seconds`, `baseline_rms` e `baseline_reference`;
os dois últimos devem ser fornecidos juntos. Aplica os mesmos limites do operador
e o prefixo de autorização da extensão cameras. A resposta contém as janelas;
o arquivo não é armazenado nem enviado para serviço remoto.

`tests/test_camera_audio.py` cobre o endpoint, tom sintético de 1 kHz, silêncio,
ruído normal declarado, impulso curto, saturação, estéreo em oposição de fase,
relógio, baseline, arquivos inválidos e passagem pelo operador.

## Trechos privados para revisão

A receita **Som acima da referência** oferece uma opção, inicialmente desligada,
para guardar um trecho por ocorrência. O operador `camera.audio_occurrence`
aceita `retain_audio=true`. A receita continua sendo criada desativada.
A amostra usada para calcular a referência normal não é guardada.

O trecho é o primeiro bloco analisado que contém a anomalia, com no máximo
30 segundos. Não representa toda a ocorrência nem garante cobertura anterior
ou posterior ao som. O relógio original acompanha a referência do trecho.
O histórico usa um player nativo sem reprodução automática e informa quando
o trecho está indisponível, expirou ou teve acesso negado.

As gravações ficam fora de `files`, em `private-camera-audio/clips.sqlite3`,
com acesso restrito ao usuário do processo. Não são criptografadas em repouso;
proteja o diretório de dados e seus backups. Há limites globais de64MiB e1024
arquivos lógicos, com remoção dos mais antigos quando necessário. Cada trecho
expira em24 horas. A leitura recusa trechos expirados; limpeza física ocorre
na abertura, em operações de armazenamento/leitura e a cada60 segundos enquanto
a extensão está carregada. Ao parar o servidor, a próxima abertura efetua a
limpeza pendente. SQLite usa `secure_delete` para limpar páginas liberadas.

`GET /api/cameras/audio/clips/{id}` exige acesso à extensão e leitura da câmera
registrada na gravação, verificados em cada requisição; retorna WAV com
`Cache-Control: private, no-store`. `DELETE` exige configuração dessa câmera.
Exclusão ou expiração não apagam as medidas e transcrições da notificação.
Não há URL pública nem reprodução automática. O limite pode remover um trecho
antes de24 horas; a retenção não promete disponibilidade durante todo o prazo.

A análise produz bytes WAV em artefato privado para o transporte existente.
Ocorrências e armazenamento ficam na origem; processamento remoto exige a
capacidade negociada `private_artifacts_v1`. O envelope privado continua limitado
a2MiB por pacote: blocos maiores não são transportáveis por esse contrato.
O teste de contrato não certifica uma implantação remota nem sincronização física.

Continuam abertos: qualificação por câmera, correlação semântica e deduplicação
entre fontes, corpus independente de fala/vento e localização calibrada.
Transcrição requer modelo local instalado e ativação explícita. Frequência e
relógio declarado não resolvem geometria ou sincronização sonora.
