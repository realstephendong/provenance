import * as vscode from 'vscode';
import { postCount } from './api';
import { ProvenanceCodeLensProvider } from './codelens';
import { Selection } from './types';
import { keyFor, ProvenanceViewProvider } from './view';

// How long the selection must sit still before the status bar asks the backend how
// much evidence exists. Selection changes fire on every keystroke-with-shift.
const STATUS_DEBOUNCE_MS = 700;

function serviceUrl(): string {
  return vscode.workspace
    .getConfiguration('provenance')
    .get<string>('serviceUrl', 'http://127.0.0.1:8000');
}

/** Build a request from an editor + range. Returns undefined when there is nothing useful to send. */
function selectionFrom(
  editor: vscode.TextEditor,
  range: vscode.Range,
): Selection | undefined {
  const code = editor.document.getText(range);
  if (code.trim().length === 0) { return undefined; }

  // No workspace folder open: repo_root is empty and the backend's git calls fail
  // safely to an empty BlameInfo. Retrieval still runs.
  const folder = vscode.workspace.getWorkspaceFolder(editor.document.uri);

  return {
    code,
    file_path: vscode.workspace.asRelativePath(editor.document.uri, false),
    repo_root: folder?.uri.fsPath ?? '',
    line_start: range.start.line + 1,   // vscode is 0-indexed; the API is 1-indexed
    line_end: range.end.line + 1,
    language: editor.document.languageId,
  };
}

function activeSelection(): Selection | undefined {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage('Provenance: open a file and select some code first.');
    return undefined;
  }
  if (editor.selection.isEmpty) {
    vscode.window.showWarningMessage('Provenance: select the code you want explained.');
    return undefined;
  }
  const selection = selectionFrom(editor, editor.selection);
  if (!selection) {
    vscode.window.showWarningMessage('Provenance: that selection is only whitespace.');
    return undefined;
  }
  return selection;
}

/**
 * Ambient discovery: while a selection sits idle, ask the cheap /context/count path
 * whether any evidence exists and advertise it in the status bar. Answers are cached
 * by selection identity so moving back and forth costs nothing.
 */
class StatusIndicator {
  private readonly item: vscode.StatusBarItem;
  private readonly cache = new Map<string, string>();
  private timer: NodeJS.Timeout | undefined;
  private token = 0;

  constructor() {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    this.item.command = 'provenance.explain';
  }

  schedule(): void {
    if (this.timer) { clearTimeout(this.timer); }
    this.timer = setTimeout(() => void this.refresh(), STATUS_DEBOUNCE_MS);
  }

  private async refresh(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.selection.isEmpty || editor.document.uri.scheme !== 'file') {
      this.item.hide();
      return;
    }
    const selection = selectionFrom(editor, editor.selection);
    if (!selection || !selection.repo_root) {
      this.item.hide();
      return;
    }

    const key = keyFor(selection);
    const cached = this.cache.get(key);
    if (cached !== undefined) {
      this.show(cached);
      return;
    }

    // Only the newest in-flight probe may paint; an older one resolving late must not.
    const mine = ++this.token;
    try {
      const count = await postCount(serviceUrl(), selection);
      if (mine !== this.token) { return; }
      const label = count.count > 0
        ? `$(git-commit) ${count.count} discussion${count.count === 1 ? '' : 's'}` +
          `${count.has_exact ? ' · exact' : ''}`
        : '';
      this.cache.set(key, label);
      this.show(label);
    } catch {
      if (mine !== this.token) { return; }
      this.item.hide();   // the status bar must never surface a backend error
    }
  }

  private show(label: string): void {
    if (!label) {
      this.item.hide();
      return;
    }
    this.item.text = label;
    this.item.tooltip = 'Provenance: explain this selection';
    this.item.show();
  }

  dispose(): void {
    if (this.timer) { clearTimeout(this.timer); }
    this.item.dispose();
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const view = new ProvenanceViewProvider(serviceUrl);
  const codeLensProvider = new ProvenanceCodeLensProvider(context.subscriptions);
  const status = new StatusIndicator();

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(ProvenanceViewProvider.viewType, view, {
      webviewOptions: { retainContextWhenHidden: true },
    }),

    vscode.commands.registerCommand('provenance.explain', async () => {
      const selection = activeSelection();
      if (selection) { await view.explain(selection); }
    }),

    // CodeLens-driven: the range comes from the lens, not the cursor.
    vscode.commands.registerCommand('provenance.explainRange', async (selection: Selection) => {
      if (selection?.code) { await view.explain(selection); }
    }),

    vscode.commands.registerCommand('provenance.refresh', async () => {
      const selection = activeSelection();
      if (selection) { await view.explain(selection, true); }
    }),

    vscode.window.onDidChangeTextEditorSelection(() => status.schedule()),
    vscode.window.onDidChangeActiveTextEditor(() => status.schedule()),

    vscode.languages.registerCodeLensProvider({ scheme: 'file' }, codeLensProvider),
    codeLensProvider,
    status,
    view,
  );

  status.schedule();
}

export function deactivate(): void {
  // Nothing to tear down: every listener is registered on context.subscriptions.
}
