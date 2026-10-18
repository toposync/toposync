# Inspeção passiva no player

No player existente, use **Mostrar análise** e escolha um fluxo. O painel observa a execução que já existe. Ele não ativa o fluxo, não solicita inferência, não ignora filtros e não executa notificações ou controles de câmera.

O quadro analisado aparece separado do vídeo, com observações brutas do detector, anteriores aos seus filtros internos, escore, origem e detalhes de tempo/modelo/revisão. As caixas pertencem à imagem exibida pelo painel. Os contadores das etapas descrevem os pacotes do fluxo, separadamente dessas observações. Não há overlay sobre vídeo ao vivo: a correspondência entre captura e apresentação ainda não foi demonstrada.

As etapas mostram contagens de recebimento, produção e filtragem, além de espera, processamento, conclusão, rejeição, erro ou encerramento. Ausência de entrada não prova uma causa. Um retorno vazio sem motivo instrumentado é apresentado como conclusão com motivo desconhecido. O filtro por categoria fornece uma razão explícita.

## Suporte desta fatia

- Runtime local, fonte, `vision.detect` e filtro por categoria. Laços próprios têm somente os pontos comuns que efetivamente utilizam.
- Quadros de três canais `uint8` realmente usados pela inferência, inclusive recorte, com cópia feita fora do callback. Transformação para a fonte desconhecida permanece desconhecida.
- Eventos sem imagem não ganham caixas ou posições inventadas.
- As camadas estruturadas de pose, rosto, identidade, embeddings e máscaras não são exportadas nesta fatia. A imagem autorizada continua privada e pode conter pessoas; ela não é anonimizada. Um escore de detecção não é uma probabilidade de reconhecimento.
- Movimento possui atual e máximo da janela observada de um segundo, agregado antes das consultas, com contagem de amostras. A janela não prolonga sujeitos.
- Produtores de revisão antiga, resultados fora de ordem, gerações encerradas e callbacks posteriores ao desligamento não renovam a visualização.

## Acesso e limites

O conteúdo é diagnóstico privado e exige proprietário autenticado. Outros papéis não recebem dados nesta fatia. Cada consulta e entrega de imagem revalida acesso e revisão do fluxo; o token da sessão não substitui autorização. A revisão de política não possui um identificador próprio e é declarada desconhecida; a autorização corrente continua obrigatória.

O painel usa snapshots completos, substituindo o estado anterior, sem uma fila de deltas a recuperar. Há no máximo 16 sessões, 128 etapas e 256 KiB de estado por sessão; imagens retidas compartilham um teto de 8 MiB. Somente dois encoders podem trabalhar simultaneamente. As cópias transitórias da inferência e a resposta codificada são adicionais ao estado retido.

Resultados visuais expiram em até um segundo, sem renovar a idade da captura. A sessão sem consultas termina em três segundos. Fechar o painel ou ocultar a aba libera a sessão visual; a análise continua. Uma alteração de configuração exige reabrir a inspeção. Falta de instrumento, capacidade ou permissão é indicada, sem reconstruir o pipeline para preencher o painel.

Consultas pendentes são canceladas ao fechar, ocultar ou trocar a seleção e têm prazo de 2,5 segundos. Falhas encerram a consulta sem repetição automática. A proteção contra sequências antigas permanece após a expiração visual; até 128 pares etapa/origem são acompanhados por sessão.

## API local

- `POST /api/pipelines/{nome}/inspection`: abre uma sessão passiva com snapshot inicial.
- `GET /api/pipeline-inspection/{sessao}`: snapshot completo atual.
- `GET /api/pipeline-inspection/{sessao}/images/{etapa}/{sequencia}`: imagem pareada, somente enquanto válida.
- `DELETE /api/pipeline-inspection/{sessao}`: libera a sessão.

As respostas usam `Cache-Control: no-store`. URLs do frontend preservam o caminho de ingresso do Home Assistant. Runtime remoto e alinhamento de transportes de vídeo não são certificados por este painel.
