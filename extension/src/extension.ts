import * as vscode from "vscode";
import { HindsightCodeLensProvider, toSelection } from "./codelens";
import { HindsightPanel } from "./panel";
import { ContextResponse, Selection } from "./types";

// The extension is deliberately thin: it collects a selection, posts JSON,
// and renders a webview. No retrieval logic lives here.

export function activate(context: vscode.ExtensionContext) {
  context.subscriptions.push(
    vscode.commands.registerCommand("hindsight.explain", explain),
    // Invoked by a CodeLens, which already knows the range.
    vscode.commands.registerCommand(
      "hindsight.explainRange",
      (startLine: number, endLine: number) => {
        const doc = vscode.window.activeTextEditor?.document;
        if (doc) {
          void lookup(toSelection(doc, startLine, endLine));
        }
      }
    ),
    vscode.languages.registerCodeLensProvider(
      { scheme: "file" },
      new HindsightCodeLensProvider()
    )
  );
}

export function deactivate() {}

async function explain() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage("Hindsight: open a file first.");
    return;
  }

  const selection = buildSelection(editor);
  if (!selection) {
    vscode.window.showWarningMessage(
      "Hindsight: select the code you want explained (a function or a block works best)."
    );
    return;
  }

  await lookup(selection);
}

async function lookup(selection: Selection) {
  const panel = HindsightPanel.show(selection);
  const base = vscode.workspace
    .getConfiguration("hindsight")
    .get<string>("serviceUrl", "http://127.0.0.1:8000");

  try {
    const data = await postContext(base, selection);
    panel.showResult(data, selection);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    panel.showError(
      `Could not reach the Hindsight service at ${base}.\n\n${message}\n\n` +
        `Start it with: make serve`
    );
  }
}

function buildSelection(editor: vscode.TextEditor): Selection | undefined {
  const sel = editor.selection;
  if (sel.isEmpty) {
    return undefined;
  }

  const code = editor.document.getText(sel);
  if (!code.trim()) {
    return undefined;
  }

  const folder = vscode.workspace.getWorkspaceFolder(editor.document.uri);
  return {
    code,
    file_path: vscode.workspace.asRelativePath(editor.document.uri, false),
    repo_root: folder ? folder.uri.fsPath : "",
    line_start: sel.start.line + 1,
    line_end: sel.end.line + 1,
    language: editor.document.languageId,
  };
}

async function postContext(
  base: string,
  selection: Selection
): Promise<ContextResponse> {
  const controller = new AbortController();
  // The budget is 8 seconds; 30 is the "something is wrong" ceiling.
  const timer = setTimeout(() => controller.abort(), 30_000);

  try {
    const resp = await fetch(`${base}/context`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(selection),
      signal: controller.signal,
    });
    if (!resp.ok) {
      throw new Error(`service returned ${resp.status} ${resp.statusText}`);
    }
    return (await resp.json()) as ContextResponse;
  } finally {
    clearTimeout(timer);
  }
}
