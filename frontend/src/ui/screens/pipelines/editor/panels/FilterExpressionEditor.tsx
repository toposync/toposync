import Editor, { type Monaco } from "@monaco-editor/react";
import React from "react";
import type * as MonacoEditor from "monaco-editor";

import { i18n } from "../../../../../util/i18n";
import { validateFilterExpression, type FilterExpressionValidationMarker } from "../../../../../util/api";
import type { SelectOption } from "../../types";
import type { FilterExpressionPathSuggestion } from "./filterExpressionContext";

const FILTER_LANGUAGE_ID = "toposync-filter";
const FILTER_MARKER_OWNER = "toposync-filter-validation";

type CompletionContext = {
  artifactSuggestions: SelectOption[];
  payloadPathSuggestions: FilterExpressionPathSuggestion[];
  metadataPathSuggestions: FilterExpressionPathSuggestion[];
};

const completionContext: CompletionContext = {
  artifactSuggestions: [],
  payloadPathSuggestions: [],
  metadataPathSuggestions: [],
};

let filterLanguageRegistered = false;
let filterCompletionProvider: MonacoEditor.IDisposable | null = null;

function setCompletionContext(context: CompletionContext): void {
  completionContext.artifactSuggestions = [...context.artifactSuggestions];
  completionContext.payloadPathSuggestions = [...context.payloadPathSuggestions];
  completionContext.metadataPathSuggestions = [...context.metadataPathSuggestions];
}

type PathRoot = "payload" | "metadata";

type PathEditContext = {
  root: PathRoot;
  token: string;
  parentPath: string | null;
  replaceRange: MonacoEditor.IRange;
  segmentRange: MonacoEditor.IRange;
};

function uniqueByPath(
  items: FilterExpressionPathSuggestion[],
  root: PathRoot,
): FilterExpressionPathSuggestion[] {
  const out: FilterExpressionPathSuggestion[] = [];
  const seen = new Set<string>();
  for (const item of items) {
    const path = String(item.path || "").trim();
    if (!path.startsWith(`${root}.`) && path !== root) continue;
    if (seen.has(path)) continue;
    seen.add(path);
    out.push({ ...item, path, detail: String(item.detail || "").trim() });
  }
  return out;
}

function extractPathEditContext(
  model: MonacoEditor.editor.ITextModel,
  position: MonacoEditor.Position,
): PathEditContext | null {
  const text = model.getValueInRange({
    startLineNumber: 1,
    startColumn: 1,
    endLineNumber: position.lineNumber,
    endColumn: position.column,
  });
  let startIndex = text.length;
  while (startIndex > 0 && /[A-Za-z0-9_.\[\]]/.test(text[startIndex - 1] || "")) {
    startIndex -= 1;
  }
  const token = text.slice(startIndex);
  if (!token) return null;

  const root = token.startsWith("payload") ? "payload" : token.startsWith("metadata") ? "metadata" : null;
  if (!root) return null;

  const baseColumn = position.column - token.length;
  const lastDotIndex = token.lastIndexOf(".");
  const segmentStartColumn = lastDotIndex >= 0 ? baseColumn + lastDotIndex + 2 : baseColumn + 1;
  const replaceStartColumn = baseColumn + 1;
  const parentPath = lastDotIndex >= 0 ? token.slice(0, lastDotIndex) : null;

  return {
    root,
    token,
    parentPath,
    replaceRange: {
      startLineNumber: position.lineNumber,
      endLineNumber: position.lineNumber,
      startColumn: replaceStartColumn,
      endColumn: position.column,
    },
    segmentRange: {
      startLineNumber: position.lineNumber,
      endLineNumber: position.lineNumber,
      startColumn: segmentStartColumn,
      endColumn: position.column,
    },
  };
}

function childSegmentFor(parentPath: string, fullPath: string): string | null {
  if (fullPath === parentPath) return null;
  if (!fullPath.startsWith(parentPath)) return null;
  const rest = fullPath.slice(parentPath.length);
  if (!rest) return null;

  if (rest.startsWith(".")) {
    const next = rest.slice(1).match(/^[A-Za-z_][A-Za-z0-9_]*/)?.[0] ?? "";
    return next || null;
  }
  if (rest.startsWith("[")) {
    const closingIndex = rest.indexOf("]");
    if (closingIndex > 0) return rest.slice(0, closingIndex + 1);
  }
  return null;
}

function formatPathSuggestionDetail(item: FilterExpressionPathSuggestion): string {
  const parts = [item.valueType, item.detail].map((value) => String(value || "").trim()).filter(Boolean);
  return parts.join(" • ");
}

function formatPathSuggestionDocumentation(item: FilterExpressionPathSuggestion): string | undefined {
  const parts: string[] = [];
  if (item.description) parts.push(item.description);
  if (item.enumValues.length > 0) parts.push(`Values: ${item.enumValues.join(", ")}`);
  if (item.examples.length > 0) parts.push(`Examples: ${item.examples.join(" | ")}`);
  return parts.length > 0 ? parts.join("\n\n") : undefined;
}

function ensureFilterLanguage(monaco: Monaco): void {
  if (!filterLanguageRegistered) {
    filterLanguageRegistered = true;
    monaco.languages.register({ id: FILTER_LANGUAGE_ID });
    monaco.languages.setMonarchTokensProvider(FILTER_LANGUAGE_ID, {
      tokenizer: {
        root: [
          [/\b(and|or|not|in|is)\b/, "keyword"],
          [/\b(True|False|None)\b/, "constant.language"],
          [/\b(payload|metadata|stream_id|lifecycle|artifacts)\b/, "variable.predefined"],
          [/[A-Za-z_][A-Za-z0-9_]*/, "identifier"],
          { include: "@whitespace" },
          [/\d+(\.\d+)?/, "number"],
          [/(==|!=|<=|>=|<|>)/, "operator"],
          [/[+\-*/%]/, "operator"],
          [/[{}()[\]]/, "@brackets"],
          [/[:,.]/, "delimiter"],
          [/"/, { token: "string.quote", next: "@stringDouble" }],
          [/'/, { token: "string.quote", next: "@stringSingle" }],
        ],
        whitespace: [[/\s+/, "white"]],
        stringDouble: [
          [/[^\\"]+/, "string"],
          [/\\./, "string.escape"],
          [/"/, { token: "string.quote", next: "@pop" }],
        ],
        stringSingle: [
          [/[^\\']+/, "string"],
          [/\\./, "string.escape"],
          [/'/, { token: "string.quote", next: "@pop" }],
        ],
      },
    });
    monaco.languages.setLanguageConfiguration(FILTER_LANGUAGE_ID, {
      autoClosingPairs: [
        { open: "(", close: ")" },
        { open: "[", close: "]" },
        { open: "{", close: "}" },
        { open: '"', close: '"' },
        { open: "'", close: "'" },
      ],
      surroundingPairs: [
        { open: "(", close: ")" },
        { open: "[", close: "]" },
        { open: "{", close: "}" },
        { open: '"', close: '"' },
        { open: "'", close: "'" },
      ],
      brackets: [
        ["(", ")"],
        ["[", "]"],
        ["{", "}"],
      ],
    });
  }

  if (filterCompletionProvider) return;

  filterCompletionProvider = monaco.languages.registerCompletionItemProvider(FILTER_LANGUAGE_ID, {
    triggerCharacters: [".", '"', "'", "[", " "],
    provideCompletionItems(model: MonacoEditor.editor.ITextModel, position: MonacoEditor.Position) {
      const word = model.getWordUntilPosition(position);
      const range = {
        startLineNumber: position.lineNumber,
        endLineNumber: position.lineNumber,
        startColumn: word.startColumn,
        endColumn: word.endColumn,
      };

      const suggestions: MonacoEditor.languages.CompletionItem[] = [];
      const seenSuggestionKeys = new Set<string>();
      const artifactSuggestions = completionContext.artifactSuggestions;
      const payloadPathSuggestions = completionContext.payloadPathSuggestions;
      const metadataPathSuggestions = completionContext.metadataPathSuggestions;
      const inString = isInsideString(model, position);
      const pathEditContext = extractPathEditContext(model, position);

      const pushSuggestion = (suggestion: MonacoEditor.languages.CompletionItem): void => {
        const key = `${String(suggestion.label)}::${String(suggestion.insertText ?? "")}`;
        if (seenSuggestionKeys.has(key)) return;
        seenSuggestionKeys.add(key);
        suggestions.push(suggestion);
      };

      const pushKeyword = (label: string): void => {
        pushSuggestion({
          label,
          kind: monaco.languages.CompletionItemKind.Keyword,
          insertText: label,
          range,
        });
      };

      const pushVariable = (label: string, detail: string): void => {
        pushSuggestion({
          label,
          kind: monaco.languages.CompletionItemKind.Variable,
          detail,
          insertText: label,
          range,
        });
      };

      const pushSnippet = (label: string, detail: string, insertText: string): void => {
        pushSuggestion({
          label,
          kind: monaco.languages.CompletionItemKind.Snippet,
          detail,
          insertText,
          insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
          range,
        });
      };

      pushVariable("payload", "Root payload object");
      pushVariable("metadata", "Root metadata object");
      pushVariable("stream_id", "Current stream identifier");
      pushVariable("lifecycle", "Current packet lifecycle");
      pushVariable("artifacts", "Set of artifact names present in the packet");

      pushKeyword("and");
      pushKeyword("or");
      pushKeyword("not");
      pushKeyword("in");
      pushKeyword("is");
      pushKeyword("True");
      pushKeyword("False");
      pushKeyword("None");

      pushSnippet('lifecycle == "open"', "Lifecycle equality check", 'lifecycle == "${1:open}"');
      pushSnippet('payload["field"]', "Payload subscript access", 'payload["${1:field}"]');
      pushSnippet('metadata["field"]', "Metadata subscript access", 'metadata["${1:field}"]');
      pushSnippet("not (...)", "Negate an expression", "not (${1:condition})");

      if (!inString) {
        const contextualPaths = [
          ...uniqueByPath(payloadPathSuggestions, "payload"),
          ...uniqueByPath(metadataPathSuggestions, "metadata"),
        ];

        if (pathEditContext) {
          const rootSuggestions = pathEditContext.root === "payload" ? payloadPathSuggestions : metadataPathSuggestions;
          const normalizedParentPath =
            pathEditContext.parentPath ??
            (pathEditContext.token === pathEditContext.root || pathEditContext.token.startsWith(`${pathEditContext.root}.`)
              ? pathEditContext.root
              : null);
          const childSegments = new Map<string, FilterExpressionPathSuggestion>();

          if (normalizedParentPath && pathEditContext.token !== pathEditContext.root) {
            for (const item of uniqueByPath(rootSuggestions, pathEditContext.root)) {
              const segment = childSegmentFor(normalizedParentPath, item.path);
              if (!segment || childSegments.has(segment)) continue;
              childSegments.set(segment, item);
            }
          }

          for (const [segment, item] of childSegments.entries()) {
            pushSuggestion({
              label: segment,
              kind: monaco.languages.CompletionItemKind.Field,
              detail: formatPathSuggestionDetail(item),
              documentation: formatPathSuggestionDocumentation(item),
              insertText: segment,
              range: pathEditContext.segmentRange,
              sortText: `0_${segment}`,
            });
          }

          for (const item of uniqueByPath(rootSuggestions, pathEditContext.root)) {
            pushSuggestion({
              label: item.path,
              kind: monaco.languages.CompletionItemKind.Property,
              detail: formatPathSuggestionDetail(item),
              documentation: formatPathSuggestionDocumentation(item),
              insertText: item.path,
              range: pathEditContext.replaceRange,
              sortText: `1_${item.path}`,
            });
          }
        } else {
          for (const item of contextualPaths) {
            pushSuggestion({
              label: item.path,
              kind: monaco.languages.CompletionItemKind.Property,
              detail: formatPathSuggestionDetail(item),
              documentation: formatPathSuggestionDocumentation(item),
              insertText: item.path,
              range,
              sortText: `1_${item.path}`,
            });
          }
        }
      }

      for (const option of artifactSuggestions) {
        const artifactName = String(option.value || "").trim();
        if (!artifactName) continue;

        if (inString) {
          pushSuggestion({
            label: artifactName,
            kind: monaco.languages.CompletionItemKind.Value,
            detail: String(option.label || artifactName),
            insertText: artifactName,
            range,
          });
          continue;
        }

        pushSuggestion({
          label: `artifact: ${artifactName}`,
          kind: monaco.languages.CompletionItemKind.Snippet,
          detail: String(option.label || artifactName),
          insertText: `"${artifactName}" in artifacts`,
          range,
        });
      }

      return { suggestions };
    },
  });
}

function isInsideString(model: MonacoEditor.editor.ITextModel, position: MonacoEditor.Position): boolean {
  const text = model.getValueInRange({
    startLineNumber: 1,
    startColumn: 1,
    endLineNumber: position.lineNumber,
    endColumn: position.column,
  });
  let quote: '"' | "'" | null = null;
  let escaped = false;
  for (const char of text) {
    if (escaped) {
      escaped = false;
      continue;
    }
    if (char === "\\") {
      escaped = true;
      continue;
    }
    if (quote) {
      if (char === quote) quote = null;
      continue;
    }
    if (char === '"' || char === "'") {
      quote = char;
    }
  }
  return quote !== null;
}

function buildMarker(
  monaco: Monaco,
  marker: FilterExpressionValidationMarker,
  message: string,
): MonacoEditor.editor.IMarkerData {
  return {
    severity: monaco.MarkerSeverity.Error,
    message,
    startLineNumber: Math.max(1, Number(marker.start_line_number || 1)),
    startColumn: Math.max(1, Number(marker.start_column || 1)),
    endLineNumber: Math.max(1, Number(marker.end_line_number || marker.start_line_number || 1)),
    endColumn: Math.max(
      Math.max(1, Number(marker.start_column || 1)) + 1,
      Number(marker.end_column || 1),
    ),
  };
}

type Props = {
  value: string;
  artifactSuggestions: SelectOption[];
  payloadPathSuggestions: FilterExpressionPathSuggestion[];
  metadataPathSuggestions: FilterExpressionPathSuggestion[];
  onChange: (value: string) => void;
};

export function FilterExpressionEditor({
  value,
  artifactSuggestions,
  payloadPathSuggestions,
  metadataPathSuggestions,
  onChange,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const editorRef = React.useRef<MonacoEditor.editor.IStandaloneCodeEditor | null>(null);
  const monacoRef = React.useRef<Monaco | null>(null);
  const [editorReadyNonce, setEditorReadyNonce] = React.useState(0);
  const [validationError, setValidationError] = React.useState<string | null>(null);
  const [validationMarker, setValidationMarker] = React.useState<FilterExpressionValidationMarker | null>(null);

  React.useEffect(() => {
    setCompletionContext({ artifactSuggestions, payloadPathSuggestions, metadataPathSuggestions });
  }, [artifactSuggestions, metadataPathSuggestions, payloadPathSuggestions]);

  React.useEffect(() => {
    const monaco = monacoRef.current;
    const model = editorRef.current?.getModel();
    if (!monaco || !model) return;

    if (!validationError || !validationMarker) {
      monaco.editor.setModelMarkers(model, FILTER_MARKER_OWNER, []);
      return;
    }

    monaco.editor.setModelMarkers(model, FILTER_MARKER_OWNER, [buildMarker(monaco, validationMarker, validationError)]);
  }, [validationError, validationMarker]);

  React.useEffect(() => {
    const monaco = monacoRef.current;
    const model = editorRef.current?.getModel();
    if (!monaco || !model) return;

    if (!String(value || "").trim()) {
      setValidationError(null);
      setValidationMarker(null);
      monaco.editor.setModelMarkers(model, FILTER_MARKER_OWNER, []);
      return;
    }

    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => {
      void validateFilterExpression(value, { signal: controller.signal })
        .then((result) => {
          if (controller.signal.aborted) return;
          if (result.ok) {
            setValidationError(null);
            setValidationMarker(null);
            return;
          }
          setValidationError(String(result.error || ""));
          setValidationMarker(result.marker ?? null);
        })
        .catch((error: any) => {
          if (controller.signal.aborted) return;
          setValidationMarker(null);
          setValidationError(
            t(
              "core.ui.pipelines.panels.filter.expression_validation_unavailable",
              { error: String(error?.message ?? error) },
              "Validation unavailable: {{error}}",
            ),
          );
        });
    }, 180);

    return () => {
      window.clearTimeout(timeoutId);
      controller.abort();
    };
  }, [editorReadyNonce, t, value]);

  const handleBeforeMount = React.useCallback(
    (monaco: Monaco) => {
      setCompletionContext({ artifactSuggestions, payloadPathSuggestions, metadataPathSuggestions });
      ensureFilterLanguage(monaco);
    },
    [artifactSuggestions, metadataPathSuggestions, payloadPathSuggestions],
  );

  const handleMount = React.useCallback(
    (editor: MonacoEditor.editor.IStandaloneCodeEditor, monaco: Monaco) => {
      editorRef.current = editor;
      monacoRef.current = monaco;
      setCompletionContext({ artifactSuggestions, payloadPathSuggestions, metadataPathSuggestions });
      ensureFilterLanguage(monaco);
      setEditorReadyNonce((prev) => prev + 1);
    },
    [artifactSuggestions, metadataPathSuggestions, payloadPathSuggestions],
  );

  React.useEffect(
    () => () => {
      const monaco = monacoRef.current;
      const model = editorRef.current?.getModel();
      if (monaco && model) {
        monaco.editor.setModelMarkers(model, FILTER_MARKER_OWNER, []);
      }
      editorRef.current = null;
      monacoRef.current = null;
    },
    [],
  );

  return (
    <div className="pipelinesFilterExpressionEditor">
      <div className="pipelinesMonacoWrap pipelinesMonacoWrapCompact">
        <Editor
          height="188px"
          language={FILTER_LANGUAGE_ID}
          value={value}
          beforeMount={handleBeforeMount}
          onMount={handleMount}
          onChange={(nextValue) => onChange(String(nextValue ?? ""))}
          options={{
            automaticLayout: true,
            fontSize: 13,
            minimap: { enabled: false },
            lineNumbers: "off",
            glyphMargin: false,
            folding: false,
            lineDecorationsWidth: 0,
            lineNumbersMinChars: 0,
            overviewRulerLanes: 0,
            hideCursorInOverviewRuler: true,
            scrollBeyondLastLine: false,
            wordWrap: "on",
            quickSuggestions: true,
            suggestOnTriggerCharacters: true,
            snippetSuggestions: "inline",
            tabCompletion: "on",
            padding: { top: 12, bottom: 12 },
          }}
        />
      </div>
      <div className="pipelinesStepHint">
        {t(
          "core.ui.pipelines.panels.filter.expression_autocomplete_hint",
          {},
          "Press Ctrl+Space to insert safe names, operators, and artifact checks.",
        )}
      </div>
      {validationError ? <div className="pipelinesInlineError">{validationError}</div> : null}
    </div>
  );
}
