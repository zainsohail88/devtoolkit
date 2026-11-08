// DevToolkit's YAML editor: CodeMirror 6, bundled by esbuild into static/vendor/editor.js.
// Everything visual comes from the design tokens (CSS custom properties), so the editor
// follows the light/dark theme like the rest of the page.
import { EditorState } from "@codemirror/state";
import {
  EditorView,
  keymap,
  lineNumbers,
  highlightActiveLine,
  highlightActiveLineGutter,
  drawSelection,
  dropCursor,
  rectangularSelection,
  placeholder as placeholderExt,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import {
  syntaxHighlighting,
  HighlightStyle,
  indentOnInput,
  bracketMatching,
  foldGutter,
  indentUnit,
} from "@codemirror/language";
import { tags } from "@lezer/highlight";
import { lintGutter, setDiagnostics } from "@codemirror/lint";
import { yaml } from "@codemirror/lang-yaml";

const theme = EditorView.theme({
  "&": {
    backgroundColor: "var(--color-surface-sunken)",
    color: "var(--color-ink)",
    fontSize: "0.875rem",
    borderRadius: "var(--radius-md)",
    border: "1px solid var(--color-hairline)",
  },
  "&.cm-focused": { outline: "2px solid var(--color-focus)", outlineOffset: "2px" },
  ".cm-scroller": { fontFamily: "var(--font-mono)", lineHeight: "1.6" },
  ".cm-content": { padding: "12px 0", caretColor: "var(--color-accent)" },
  ".cm-gutters": {
    backgroundColor: "var(--color-surface-sunken)",
    color: "var(--color-ink-muted)",
    border: "none",
    borderRight: "1px solid var(--color-divider)",
  },
  ".cm-activeLine": { backgroundColor: "var(--color-accent-soft)" },
  ".cm-activeLineGutter": { backgroundColor: "var(--color-accent-soft)", color: "var(--color-ink)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": {
    backgroundColor: "var(--color-accent-soft) !important",
  },
  ".cm-cursor": { borderLeftColor: "var(--color-accent)" },
  ".cm-matchingBracket": { outline: "1px solid var(--color-accent)", backgroundColor: "transparent" },
  ".cm-placeholder": { color: "var(--color-ink-muted)" },
  ".cm-lintRange-error": {
    backgroundImage: "none",
    backgroundColor: "var(--color-danger-soft)",
    textDecoration: "underline wavy var(--color-danger)",
  },
  ".cm-lint-marker-error": { content: "none" },
  ".cm-tooltip": {
    backgroundColor: "var(--color-surface-raised)",
    color: "var(--color-ink)",
    border: "1px solid var(--color-hairline)",
    borderRadius: "var(--radius-sm)",
  },
  ".cm-diagnostic-error": { borderLeftColor: "var(--color-danger)" },
  ".cm-foldGutter .cm-gutterElement": { color: "var(--color-ink-muted)" },
});

const highlight = HighlightStyle.define([
  { tag: [tags.propertyName, tags.definition(tags.propertyName)], color: "var(--color-syntax-key)" },
  { tag: [tags.string, tags.special(tags.string)], color: "var(--color-syntax-string)" },
  { tag: [tags.number, tags.integer, tags.float], color: "var(--color-syntax-number)" },
  { tag: [tags.bool, tags.null, tags.atom, tags.keyword], color: "var(--color-syntax-bool)" },
  { tag: [tags.comment, tags.meta], color: "var(--color-syntax-comment)", fontStyle: "italic" },
  { tag: [tags.punctuation, tags.separator, tags.bracket], color: "var(--color-syntax-punct)" },
  { tag: tags.invalid, color: "var(--color-danger)" },
]);

/**
 * Create an editor in `parent`.
 * options: { doc, readOnly, placeholder, onChange(text) }
 * returns:  { view, getValue(), setValue(text), setError(line, column, message), clearErrors(), focus() }
 */
export function create(parent, options = {}) {
  const extensions = [
    lineNumbers(),
    highlightActiveLineGutter(),
    foldGutter(),
    lintGutter(),
    history(),
    drawSelection(),
    dropCursor(),
    rectangularSelection(),
    indentOnInput(),
    bracketMatching(),
    highlightActiveLine(),
    indentUnit.of("  "),
    EditorState.tabSize.of(2),
    EditorView.lineWrapping,
    keymap.of([indentWithTab, ...defaultKeymap, ...historyKeymap]),
    yaml(),
    syntaxHighlighting(highlight),
    theme,
  ];
  if (options.placeholder) extensions.push(placeholderExt(options.placeholder));
  if (options.readOnly) extensions.push(EditorState.readOnly.of(true), EditorView.editable.of(false));
  if (options.onChange) {
    extensions.push(
      EditorView.updateListener.of((update) => {
        if (update.docChanged) options.onChange(update.state.doc.toString());
      }),
    );
  }
  const view = new EditorView({
    state: EditorState.create({ doc: options.doc || "", extensions }),
    parent,
  });

  const api = {
    view,
    getValue: () => view.state.doc.toString(),
    setValue(text) {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
      api.clearErrors();
    },
    // Marks one position in the text with a red diagnostic and scrolls it into view.
    setError(line, column, message) {
      const doc = view.state.doc;
      if (!line || line < 1 || line > doc.lines) {
        api.clearErrors();
        return;
      }
      const info = doc.line(line);
      const from = Math.min(info.from + Math.max((column || 1) - 1, 0), info.to);
      const to = from < info.to ? from + 1 : info.to;
      view.dispatch(
        setDiagnostics(view.state, [{ from, to, severity: "error", message }]),
        { effects: EditorView.scrollIntoView(from, { y: "center" }) },
      );
    },
    clearErrors: () => view.dispatch(setDiagnostics(view.state, [])),
    focus: () => view.focus(),
  };
  return api;
}

window.DevToolkitEditor = { create };
