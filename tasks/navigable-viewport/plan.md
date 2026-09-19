# Área de renderização navegável

Implementar o plano solicitado usando o comportamento da planta como referência: zoom ancorado no cursor, arraste, redimensionamento sem perder o centro e navegação independente dos dados editados.

## Decisões

- Compartilhar a matemática de transformação, arraste e zoom entre `Viewport2D` e um componente DOM `NavigableViewport`, exposto por `host.ui`.
- O núcleo conhece coordenadas e limites, não câmeras. Conteúdo e ferramentas pertencem ao consumidor.
- Separar navegação de clique, seleção e recorte. Botões internos mantêm a interação; ferramentas recebem o botão esquerdo no modo de edição. Botões central/direito permitem navegar. Dois dedos navegam e cancelam um gesto de edição em andamento.
- Preservar coordenadas normalizadas, imagens originais, recortes e trabalhos paralelos. Não movimentar câmeras na validação.
- Interface enxuta: ampliar, reduzir e ajustar à área; gestos e teclado complementam os controles.

## Ordem

1. Matemática compartilhada e regressões de rotação/âncora.
2. Componente reutilizável, contrato público e integração da planta.
3. Panorâmica no mapeamento, mantendo pontos, previsão e expansão.
4. Prévia de configurações e editor de área útil.
5. Testes de interações e builds; validação na instância local.

## Riscos e verificação

O principal risco é confundir arraste de navegação com edição. Testar cliques estacionários, arrastes, cancelamento, roda do mouse, foco, teclado e coordenadas após zoom. Validar redimensionamento e alvos de interação com tamanho constante. Usar fixtures sintéticas e conferir a imagem salva na câmera Frente sem alterar seu mapeamento.

Este diretório preserva os planos incompletos de outras tarefas, conforme a proposta aceita pelo usuário ao solicitar implementação.
