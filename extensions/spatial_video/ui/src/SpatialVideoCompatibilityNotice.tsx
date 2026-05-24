import React from "react";

type Props = {
  loading: boolean;
  error: string | null;
  hasCompatibleProjection: boolean;
};

export function SpatialVideoCompatibilityNotice({ loading, error, hasCompatibleProjection }: Props): React.ReactElement | null {
  if (!loading && !error && hasCompatibleProjection) return null;

  const isEmpty = !loading && !error && !hasCompatibleProjection;
  const title = loading
    ? "Carregando transmissões mapeadas..."
    : error
      ? "Falha ao carregar transmissões"
      : "Nada compatível para projetar";
  const body = loading
    ? "Verificando câmeras com transmissão ativa e mapeamento nesta composição."
    : error
      ? error
      : "Esta visualização só mostra câmeras que tenham uma transmissão/publicação ativa e um mapeamento com pelo menos 4 pontos de controle completos.";

  return (
    <div
      role={isEmpty || error ? "alert" : "status"}
      style={{
        position: "absolute",
        inset: 0,
        display: "grid",
        placeItems: "center",
        pointerEvents: "none",
        zIndex: 5,
        padding: 24,
      }}
    >
      <div
        className="card"
        style={{
          width: "min(480px, 100%)",
          padding: 18,
          display: "grid",
          gap: 10,
          border: isEmpty ? "1px solid rgba(251,191,36,0.45)" : undefined,
          background: "rgba(15,23,42,0.86)",
          boxShadow: "0 18px 48px rgba(0,0,0,0.36)",
        }}
      >
        <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
          <div
            aria-hidden="true"
            style={{
              width: 34,
              height: 34,
              borderRadius: 999,
              display: "grid",
              placeItems: "center",
              color: error ? "rgb(254,202,202)" : isEmpty ? "rgb(253,230,138)" : "rgb(186,230,253)",
              background: error ? "rgba(127,29,29,0.72)" : isEmpty ? "rgba(120,53,15,0.64)" : "rgba(12,74,110,0.64)",
              border: "1px solid rgba(226,232,240,0.18)",
              flex: "0 0 auto",
            }}
          >
            <i className={`fa-solid fa-${loading ? "spinner" : error ? "triangle-exclamation" : "video-slash"}`} />
          </div>
          <div style={{ display: "grid", gap: 6, minWidth: 0 }}>
            <div style={{ fontWeight: 700, color: "rgba(248,250,252,0.96)" }}>{title}</div>
            <div style={{ color: "rgba(203,213,225,0.86)", lineHeight: 1.45 }}>{body}</div>
            {isEmpty ? (
              <div style={{ color: "rgba(148,163,184,0.92)", fontSize: 13, lineHeight: 1.45 }}>
                Confira se o elemento de câmera desta composição está vinculado à câmera correta, se existe uma transmissão ativa para ela e se o conjunto de pontos selecionado está completo.
              </div>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}
