import * as vscode from 'vscode';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { postCount } from './api';
import { ProvenanceCodeLensProvider } from './codelens';
import { Selection } from './types';
import { keyFor, ProvenanceViewProvider } from './view';

// How long the selection must sit still before the status bar asks the backend how
// much evidence exists. Selection changes fire on every keystroke-with-shift.
const STATUS_DEBOUNCE_MS = 700;
const execFileAsync = promisify(execFile);
const repositoryCache = new Map<string, Promise<string>>();

async function githubRepository(repoRoot: string): Promise<string> {
  let pending = repositoryCache.get(repoRoot);
  if (!pending) {
    pending = execFileAsync('git', ['-C', repoRoot, 'remote', 'get-url', 'origin'])
      .then(({ stdout }) => {
        const remote = stdout.trim().replace(/\.git$/, '');
        const match = remote.match(/(?:github\.com[/:])([^/]+\/[^/]+)$/i);
        return match ? match[1] : '';
      })
      .catch(() => '');
    repositoryCache.set(repoRoot, pending);
  }
  return pending;
}

async function localBlame(selection: Selection): Promise<Record<string, unknown> | undefined> {
  if (!selection.repo_root) { return undefined; }
  try {
    const { stdout } = await execFileAsync('git', [
      '-C', selection.repo_root, 'blame', '--porcelain',
      `-L${selection.line_start},${selection.line_end}`, '--', selection.file_path,
    ]);
    const counts = new Map<string, { author: string; ts: number; lines: number }>();
    let sha = ''; let author = ''; let ts = 0;
    for (const line of stdout.split('\n')) {
      const header = line.match(/^([0-9a-f]{40}) /);
      if (header) { sha = header[1]; author = ''; ts = 0; continue; }
      if (line.startsWith('author ')) { author = line.slice(7); continue; }
      if (line.startsWith('author-time ')) { ts = Number(line.slice(12)); continue; }
      if (line.startsWith('\t') && sha) {
        const item = counts.get(sha) ?? { author, ts, lines: 0 };
        item.lines += 1; counts.set(sha, item);
      }
    }
    const commits = [...counts.entries()].map(([full, item]) => ({
      sha: full.slice(0, 7), author: item.author || null,
      date: item.ts ? new Date(item.ts * 1000).toISOString().slice(0, 10) : null,
      ts: item.ts || null, lines: item.lines, pr_number: null, dominant: false, current: true,
    }));
    const dominant = commits.sort((a, b) => b.lines - a.lines)[0];
    if (!dominant) { return undefined; }
    dominant.dominant = true;
    return { authors: [...new Set(commits.map(c => c.author).filter(Boolean))], dominant_sha: dominant.sha,
      all_shas: commits.map(c => c.sha), pr_number: null, pr_numbers: [], commit_date: dominant.date,
      commit_ts: dominant.ts, uncommitted: false, commits };
  } catch { return undefined; }
}

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

async function activeSelection(): Promise<Selection | undefined> {
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
  if (selection.repo_root) {
    selection.github_repo = await githubRepository(selection.repo_root);
    selection.precomputed_blame = await localBlame(selection);
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
      const selection = await activeSelection();
      if (selection) { await view.explain(selection); }
    }),

    // CodeLens-driven: the range comes from the lens, not the cursor.
    vscode.commands.registerCommand('provenance.explainRange', async (selection: Selection) => {
      if (selection?.code) { await view.explain(selection); }
    }),

    vscode.commands.registerCommand('provenance.refresh', async () => {
      const selection = await activeSelection();
      if (selection) { await view.explain(selection, true); }
    }),

    vscode.commands.registerCommand('provenance.openTimeline', () => {
      view.openTimeline();
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
