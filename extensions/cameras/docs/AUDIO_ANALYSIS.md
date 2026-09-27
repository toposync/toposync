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

Continuam abertos: captura real por câmera, retenção de trechos autorizados,
eventos persistentes e correlação semântica/deduplicação entre fontes,
fala/vento reais, transcrição e localização calibrada. Não existe modelo
de transcrição configurado neste fluxo. Frequência e relógio declarado não
resolvem a geometria ou sincronização exigidas para localização sonora.
