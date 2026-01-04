import React, { useEffect, useMemo, useState } from "react";

import type { Composition, CompositionSummary } from "../util/api";
import { Modal } from "./Modal";
import { Icon } from "./Icon";

type Props = {
  open: boolean;
  compositions: CompositionSummary[];
  activeCompositionId: string;
  onClose: () => void;
  onActivate: (compositionId: string) => Promise<Composition>;
  onCreate: (name: string) => Promise<Composition>;
  onRename: (compositionId: string, name: string) => Promise<Composition>;
  onDelete: (compositionId: string) => Promise<void>;
};

export function CompositionSelectorModal({
  open,
  compositions,
  activeCompositionId,
  onClose,
  onActivate,
  onCreate,
  onRename,
  onDelete,
}: Props): React.ReactElement | null {
  const [newName, setNewName] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canDelete = compositions.length > 1;

  const sorted = useMemo(
    () => [...compositions].sort((a, b) => a.name.localeCompare(b.name)),
    [compositions],
  );

  useEffect(() => {
    if (!open) return;
    setError(null);
    setConfirmDeleteId(null);
    setEditingId(null);
    setEditingName("");
  }, [open]);

  async function handleCreate() {
    const name = newName.trim();
    if (!name || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onCreate(name);
      setNewName("");
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Falha ao criar composição");
    } finally {
      setBusy(false);
    }
  }

  async function handleActivate(compositionId: string) {
    if (busy) return;
    if (compositionId === activeCompositionId) {
      onClose();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onActivate(compositionId);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Falha ao trocar composição");
    } finally {
      setBusy(false);
    }
  }

  async function handleRename(compositionId: string) {
    const name = editingName.trim();
    if (!name || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onRename(compositionId, name);
      setEditingId(null);
      setEditingName("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Falha ao renomear composição");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(compositionId: string) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await onDelete(compositionId);
      setConfirmDeleteId(null);
      setEditingId(null);
      setEditingName("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Falha ao excluir composição");
    } finally {
      setBusy(false);
    }
  }

  if (!open) return null;

  return (
    <Modal open={open} title="Composições" onClose={onClose}>
      <div className="modalSectionTitle">Nova composição</div>
      <div className="compositionCreateRow">
        <input
          className="input"
          value={newName}
          placeholder="Nome (ex: Térreo, Superior...)"
          onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void handleCreate();
          }}
          disabled={busy}
        />
        <button
          className="iconButton iconButtonPrimary"
          type="button"
          onClick={() => void handleCreate()}
          aria-label="Criar composição"
          disabled={busy || !newName.trim()}
        >
          <Icon name="plus" />
        </button>
      </div>

      <div className="sectionDivider" />

      <div className="modalSectionTitle">Suas composições</div>
      <div className="compositionList">
        {sorted.map((c) => {
          const isActive = c.id === activeCompositionId;
          const isEditing = editingId === c.id;
          const isConfirmingDelete = confirmDeleteId === c.id;

          if (isEditing) {
            return (
              <div className="compositionRow" key={c.id}>
                <div className="compositionMain">
                  <input
                    className="input"
                    value={editingName}
                    onChange={(e) => setEditingName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void handleRename(c.id);
                      if (e.key === "Escape") {
                        setEditingId(null);
                        setEditingName("");
                      }
                    }}
                    disabled={busy}
                    autoFocus
                  />
                </div>
                <div className="compositionActions">
                  <button
                    className="iconButton iconButtonPrimary"
                    type="button"
                    onClick={() => void handleRename(c.id)}
                    aria-label="Salvar nome"
                    disabled={busy || !editingName.trim()}
                  >
                    <Icon name="check" />
                  </button>
                  <button
                    className="iconButton"
                    type="button"
                    onClick={() => {
                      setEditingId(null);
                      setEditingName("");
                    }}
                    aria-label="Cancelar"
                    disabled={busy}
                  >
                    <Icon name="xmark" />
                  </button>
                </div>
              </div>
            );
          }

          const mainLabel = isConfirmingDelete ? `Excluir “${c.name}”?` : c.name;

          return (
            <div className="compositionRow" key={c.id}>
              <button
                className={`compositionSelectButton${isActive ? " isActive" : ""}${
                  isConfirmingDelete ? " isDanger" : ""
                }`}
                type="button"
                onClick={() => void handleActivate(c.id)}
                disabled={busy || isConfirmingDelete}
              >
                <span className="compositionName">{mainLabel}</span>
                {isActive && !isConfirmingDelete ? (
                  <span className="compositionBadge">
                    <Icon name="check" />
                  </span>
                ) : null}
              </button>

              <div className="compositionActions">
                {isConfirmingDelete ? (
                  <>
                    <button
                      className="iconButton"
                      type="button"
                      onClick={() => setConfirmDeleteId(null)}
                      aria-label="Cancelar exclusão"
                      disabled={busy}
                    >
                      <Icon name="xmark" />
                    </button>
                    <button
                      className="iconButton iconButtonDanger"
                      type="button"
                      onClick={() => void handleDelete(c.id)}
                      aria-label="Confirmar exclusão"
                      disabled={busy}
                    >
                      <Icon name="trash" />
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      className="iconButton"
                      type="button"
                      onClick={() => {
                        setEditingId(c.id);
                        setEditingName(c.name);
                        setConfirmDeleteId(null);
                        setError(null);
                      }}
                      aria-label="Renomear composição"
                      disabled={busy}
                    >
                      <Icon name="pen-to-square" />
                    </button>
                    <button
                      className="iconButton iconButtonDanger"
                      type="button"
                      onClick={() => {
                        if (!canDelete) return;
                        setConfirmDeleteId(c.id);
                        setEditingId(null);
                        setEditingName("");
                        setError(null);
                      }}
                      aria-label="Excluir composição"
                      disabled={busy || !canDelete}
                      title={!canDelete ? "Não é possível excluir a última composição" : undefined}
                    >
                      <Icon name="trash" />
                    </button>
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {error ? (
        <div className="errorText" role="alert">
          {error}
        </div>
      ) : null}
    </Modal>
  );
}

