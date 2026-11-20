// DevToolkit's code editor: CodeMirror 6 with YAML and HCL (Terraform) highlighting, bundled
// by esbuild into static/vendor/editor.js.
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
  StreamLanguage,
} from "@codemirror/language";
import { tags } from "@lezer/highlight";
import { lintGutter, setDiagnostics } from "@codemirror/lint";
import { yaml } from "@codemirror/lang-yaml";

// Terraform's HCL. CodeMirror has no official HCL package, and highlighting is all the output
// panels need, so a small stream tokenizer does: comments, strings with ${...}, numbers,
// true/false/null, block types at the start of a line, and attribute names before `=`.
const HCL_BLOCKS = new Set([
  "resource", "data", "variable", "output", "locals", "module", "provider", "terraform",
  "import", "moved", "removed", "check", "dynamic", "content", "lifecycle", "validation",
]);
const hcl = StreamLanguage.define({
  name: "hcl",
  startState: () => ({ inString: false, depth: 0, inComment: false }),
  token(stream, state) {
    if (state.inComment) {
      if (stream.skipTo("*/")) {
        stream.match("*/");
        state.inComment = false;
      } else {
        stream.skipToEnd();
      }
      return "comment";
    }
    if (state.inString) {
      if (state.depth > 0) {
        if (stream.match("}")) {
          state.depth -= 1;
          return "meta";
        }
      } else {
        if (stream.match("${") || stream.match("%{")) {
          state.depth += 1;
          return "meta";
        }
        while (!stream.eol()) {
          const next = stream.peek();
          if (next === '"') {
            stream.next();
            state.inString = false;
            return "string";
          }
          if ((next === "$" || next === "%") && stream.string.charAt(stream.pos + 1) === "{") break;
          if (next === "\\") stream.next();
          stream.next();
        }
        return "string";
      }
    }
    if (stream.eatSpace()) return null;
    if (stream.match("#") || stream.match("//")) {
      stream.skipToEnd();
      return "comment";
    }
    if (stream.match("/*")) {
      state.inComment = true;
      return "comment";
    }
    if (stream.peek() === '"') {
      stream.next();
      state.inString = true;
      return "string";
    }
    if (stream.match(/^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?/)) return "number";
    if (stream.match(/^(true|false|null)\b/)) return "bool";
    const word = stream.match(/^[A-Za-z_][A-Za-z0-9_-]*/);
    if (word) {
      if (stream.match(/^\s*=(?!=)/, false)) return "propertyName";
      if (HCL_BLOCKS.has(word[0]) && stream.string.slice(0, stream.start).trim() === "") return "keyword";
      return "variableName";
    }
    stream.next();
    return "punctuation";
  },
});

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
 * options: { doc, readOnly, placeholder, onChange(text), language: "yaml" | "hcl" }
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
    options.language === "hcl" ? hcl : yaml(),
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
