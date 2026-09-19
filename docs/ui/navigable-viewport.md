# Área de renderização navegável

`host.ui.NavigableViewport` oferece navegação para imagens, elementos DOM, SVG e canvas. O conteúdo define suas unidades; o componente mantém apenas centro e escala. Não conhece câmeras, recortes, pontos de calibração ou persistência.

```tsx
const navigation = useRef<NavigableViewportController | null>(null);

<host.ui.NavigableViewport
  label="Imagem de referência"
  contentKey={imageUrl}
  contentSize={{ width: 1600, height: 900 }}
  controllerRef={navigation}
  style={{ height: 400 }}
>
  <img src={imageUrl} alt="Referência" draggable={false}
       style={{ width: "100%", height: "100%" }} />
</host.ui.NavigableViewport>
```

O consumidor fornece uma altura para a área. `contentSize` usa dimensões positivas e finitas. `contentKey` ou mudança das dimensões reinicia o enquadramento. `initialBounds` permite ajustar uma região em coordenadas do conteúdo sem recortá-lo. O ajuste inicial acompanha o tamanho da área; depois de navegar, redimensionar preserva centro e escala absoluta.

## Interações

- Roda do mouse e gesto de trackpad: zoom ancorado no cursor, com normalização de pixels, linhas e páginas. A página não rola enquanto o ponteiro estiver sobre a área.
- Arraste esquerdo: navegação em `interactionMode="navigate"` (padrão). Clique com deslocamento menor que três pixels chama `onContentClick`.
- Em `interactionMode="interact"`, o botão esquerdo fica com a ferramenta do consumidor. Botões central/direito ou Espaço + arraste navegam. `onNavigationStart` cancela o gesto de edição antes de uma pinça ou navegação assumir o controle.
- Toque: arraste para navegar e dois dedos para ampliar. Depois de pinça ou cancelamento não é emitido um clique de edição.
- Com foco na área: `+`/`-` ampliam/reduzem, `0`/Home ajustam, setas deslocam. `onKeyDown` do consumidor roda primeiro e pode reservar uma tecla com `preventDefault`, como as setas e Espaço usadas para marcar pontos no mapeamento.
- Botões, campos e links internos conservam sua ação. `data-viewport-control` protege outros controles da navegação.

Ferramentas que recebem o ponteiro devem usar captura, lidar com `pointercancel`/perda de captura e implementar `onNavigationStart` para desfazer somente o gesto ainda não concluído. A roda é ignorada durante um gesto ativo para não mudar sua referência geométrica.

## Coordenadas e reutilização

O controlador expõe `fit(bounds?)`, `zoomBy(factor)`, `centerOn(point)`, `contentToScreen(point)` e `screenToContent(point)`. As coordenadas de tela são relativas à área, em pixels CSS. `onContentClick` e `onContentPointerMove` fornecem coordenadas do conteúdo e o evento original. `onViewChange` fornece centro, escala absoluta e zoom relativo ao ajuste integral da área atual.

`children` pode ser uma função que recebe o estado da visualização. A variável CSS `--viewport-inverse-scale` permite manter marcadores com tamanho constante na tela: `transform: translate(-50%, -50%) scale(var(--viewport-inverse-scale))`. A localização do marcador permanece nas unidades do conteúdo.

O canvas da planta (`Viewport2D`) usa o mesmo módulo `viewportNavigation.ts` para rotação, arraste e zoom. Suas ferramentas, seleção e desenho permanecem no componente especializado. A extensão de câmeras reutiliza o componente DOM na prévia, no mapeamento da panorâmica e no editor de área útil.

## Verificação

```sh
node --test frontend/tests/viewportNavigation.test.cjs extensions/cameras/ui/tests/*.test.cjs
npm --workspace @toposync/frontend run typecheck
npx tsc -p extensions/cameras/ui/tsconfig.json
npx playwright test --config playwright.viewport.config.js
npx playwright test --config playwright.panorama.config.js --grep 'saved source panorama starts|keyboard works|bidirectional provisional'
```

As configurações Playwright usam câmeras sintéticas e diretórios de teste. Não demonstram prontidão física nem cobertura da câmera real.
