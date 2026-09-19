# Decisão e condição de validação da entrega

Contrato copiado integralmente de `toposync-contrato-de-entrega (1).md`. As recomendações técnicas são hipóteses; a autorização de implementação e de dois ensaios vem da solicitação do usuário. Dossiê lido nas seções 5–7, 9–15, 30–34 e 46–50. Nenhuma skill adicional, alteração de controlador ou acesso físico foi realizado.

## Decisão antes de editar o produto

A política v3 impõe primeira faixa completa → altura → faixa inversa. Em `panorama_region.py`, `_region_decision` e a escolha da próxima fase demonstram a dependência; a recusa de `_verified_link` lança `coverage_connection_unverified` e sai desse caminho. A alternativa regional legada visita a altura cedo, mas também é unilateral e encerra na ligação recusada. Portanto não basta trocar a versão selecionada.

A menor direção coerente seria tornar independentes as regiões relativas à referência e visitar a altura cedo, reutilizando `_pulse`, `_move`, as referências e localização existentes. Mudança de região após recusa dependeria de recuperação visual qualificada; o ledger global não poderia ser reiniciado. Isso ainda é uma escolha candidata, não implementação ou demonstração de recuperação segura. O retorno final bem-sucedido da Garagem não prova uma trajetória alternativa que não foi executada.

Estado histórico em 14/09: a implementação e o lote ficaram suspensos pelo teste real obrigatório indisponível abaixo. Em 15/09 o usuário substituiu esse requisito por validações separadas; a implementação local pode prosseguir. Não alterar o matcher nem criar uma fixture falsa para remover esse impedimento.

## Evidência decisiva: o par rejeitado não foi recuperado

- Trabalho `2cd035bbf78b412b96481f2f5345c437`: última fotografia aceita `capture-0004.jpg`, sequência 274.
- O manifesto identifica `transition-b96a02f168004b90b769690c37b10512.jpg` como **baseline**, sequência 289, anterior ao sexto comando. `panorama_scan.py` grava esse arquivo antes do envio.
- O matcher atual aplicado aos dois arquivos disponíveis retorna **verified=true**, 580 inliers, sobreposição 0,99705054 e desvio 0,07038 px. Eles não formam a fixture negativa solicitada.
- O sexto movimento é `f6b7051a5bfe4dfcae393e8050e231a3`, pan -0,1 por 2 s. A recusa registrada é `correspondences_not_distributed`. O diagnóstico registra observação estável na sequência 344 e commit do endpoint até 347, mas não conserva os pixels da imagem selecionada retornada pelo movimento.
- Os únicos replays desse trabalho cobrem sequências 40–71 e 94–124. Não cobrem o sexto movimento. `_preserve_replay` classifica `capture_stable` como accepted e limita a dois replays desse resultado; o sexto movimento já encontrava os dois slots ocupados. A recusa posterior da política ocorre antes de `_accept`, sem persistir essa imagem.
- `return-final.jpg` é posterior ao retorno e não substitui a imagem rejeitada.

O README anterior indicava o arquivo transition como observação rejeitada. Essa interpretação está corrigida aqui; o histórico permanece preservado. Diagnósticos numéricos podem testar decisões com a recusa registrada, mas não reexecutar a fixture geométrica negativa nem certificar seus pixels.

Condição faltante: o quadro exato selecionado após o sexto comando (com identidade/proveniência), ou um replay que contenha esse quadro, além da âncora já disponível. Não foi localizado nos diretórios do trabalho, artefato e relatórios citados nem nas buscas por seus identificadores. Um novo movimento não recuperaria retroativamente essa evidência. Não há autorização para uma nova coleta diagnóstica nesta retomada; os dois ensaios de aceitação dependem dos testes prévios.

Evidência reproduzida offline, hashes, inventário, diagnóstico e resultado do matcher: `ignore/coverage-contract-20260914/evidence.json`. Naquele checkpoint ainda não havia variante implementada; o estado atual está abaixo e em checkpoint-entrega.md.

## Decisão de implementação — 15/09/2026

Uma única variante versionada: preservar a observação inicial como referência conectada, visitar a altura inferior e uma pequena expansão lateral antes de exigir a extensão final de qualquer lado; localizar na referência entre visitas e explorar os dois sentidos. Reutilizar `_pulse`, `_move`, `_current_anchor`, retorno à referência salva e `_accept`. Uma recusa geométrica fecha aquela visita; só a localização verificada e comando/Stop/observação certos permitem outra. Todas as visitas e recuperações consomem o mesmo orçamento v3, sem reinício. Manter políticas antigas para históricos. Preservar o primeiro par recusado sem perdas em arquivos privados limitados, com identidades e recibos. Percurso concluído e montagem pronta continuam distintos de aprovação visual da cobertura.

Implementado como `initial_region` v4 em `panorama_coverage.py`: inferior → pequena extensão lateral → lado oposto → extensão do primeiro lado, com localização na referência entre visitas. O sinal lateral é interpretado pela imagem; o caso espelhado passa no teste da política real. A referência inicial integral é persistida com evidência local provisória explícita e precisa pertencer ao grafo verificado. Não foi convertida em exposição fisicamente certificada.

A recuperação escolhida é uma aproximação ao destino salvo e localização pelo verificador existente, uma vez por visita. Se não localizar, encerra; não acrescenta um controlador corretivo. Isso torna a validade desse retorno intermediário uma pendência importante do aceite físico. Sem efeito vertical após as tentativas limitadas, ainda podem ser adquiridos os lados após localização; o resultado permanece parcial. Os 30 comandos e 420 s são cumulativos, com reserva de 5/150.

A v4 não implementa retomada automática de visita interrompida: conserva o ledger, originais e referências sem repetir a rota. As versões anteriores mantêm seu contrato de retomada. O encerramento por orçamento pode usar a reserva de retorno; incerteza de comando, observação ou localização admite Stop, sem recall automático. Recorte continua independente. Havendo um ativo anterior, inclusive stale, a nova reconstrução é candidata, preservando a geometria já vinculada.

Os testes locais verificam essas decisões com dados simulados. O par disponível da Garagem continua sendo regressão positiva de geometria real, não o negativo histórico. Aceite visual e capacidades de recuperação reais continuam pendentes.
