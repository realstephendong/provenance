import { createHash } from 'crypto';
import * as vscode from 'vscode';
import { postContext } from './api';
import { renderGraph } from './graph';
import { ContextResponse, Result, Selection } from './types';

// The stage list is indicative, not observed: /context is a single blocking call with
// no progress channel, so this rotates on a timer. Real per-stage numbers arrive with
// the response and are shown in the footer.
const LOADING_STAGES = [
  'Reading git history…',
  'Resolving the pull request…',
  'Describing the code…',
  'Searching Elasticsearch…',
  'Ranking discussions…',
  'Building the graph…',
  'Writing the answer…',
];
const STAGE_INTERVAL_MS = 900;
const MAX_HISTORY = 8;

interface Entry {
  key: string;
  selection: Selection;
  response: ContextResponse;
  at: number;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** Identity of a selection for cache/history purposes: same lines *and* same bytes. */
export function keyFor(selection: Selection): string {
  const digest = createHash('sha1').update(selection.code).digest('hex').slice(0, 12);
  return `${selection.file_path}:${selection.line_start}-${selection.line_end}:${digest}`;
}

function shortLabel(selection: Selection): string {
  const name = selection.file_path.split('/').pop() ?? selection.file_path;
  return `${name}:${selection.line_start}-${selection.line_end}`;
}

/** Turn [1] / [2] citations into links that scroll to the matching evidence card. */
function linkCitations(text: string, resultCount: number): string {
  const linked = escapeHtml(text).replace(/\[(\d+)\]/g, (match, digits: string) => {
    const index = Number(digits);
    if (index < 1 || index > resultCount) { return match; }
    return `<a class="citation" href="#result-${index}" data-target="result-${index}">[${index}]</a>`;
  });
  return inlineCode(linked);
}

export class ProvenanceViewProvider implements vscode.WebviewViewProvider {
  static readonly viewType = 'provenance.view';

  private view: vscode.WebviewView | undefined;
  private ready = false;
  private lastRenderHtml: string | undefined;
  private loadingTimer: NodeJS.Timeout | undefined;
  private readyWaiters: (() => void)[] = [];

  private history: Entry[] = [];
  private activeKey: string | undefined;
  private lastFailure: Selection | undefined;

  constructor(private readonly serviceUrl: () => string) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    this.ready = false;
    view.webview.options = { enableScripts: true };
    view.webview.html = this.shell(view.webview);

    view.webview.onDidReceiveMessage((message) => this.onMessage(message));
    view.onDidDispose(() => {
      this.view = undefined;
      this.ready = false;
    });
  }

  // --- webview plumbing ------------------------------------------------------

  private post(message: Record<string, unknown>): void {
    if (message.type === 'render') {
      this.lastRenderHtml = message.html as string;
    }
    if (this.view && this.ready) {
      void this.view.webview.postMessage(message);
    }
  }

  private onReady(): void {
    this.ready = true;
    // A webview view is torn down when hidden; repaint whatever was last shown.
    if (this.lastRenderHtml !== undefined) {
      void this.view?.webview.postMessage({ type: 'render', html: this.lastRenderHtml });
    } else {
      void this.view?.webview.postMessage({ type: 'render', html: emptyFragment() });
    }
    const waiters = this.readyWaiters;
    this.readyWaiters = [];
    for (const waiter of waiters) { waiter(); }
  }

  /** Bring the sidebar into view, resolving it first if it has never been opened. */
  private async reveal(): Promise<void> {
    if (!this.view) {
      await vscode.commands.executeCommand(`${ProvenanceViewProvider.viewType}.focus`);
    } else if (!this.view.visible) {
      this.view.show(true);
    }
    if (this.ready) { return; }
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, 3000);
      this.readyWaiters.push(() => { clearTimeout(timer); resolve(); });
    });
  }

  private async onMessage(message: { type: string; url?: string; key?: string }): Promise<void> {
    switch (message.type) {
      case 'ready':
        this.onReady();
        return;
      case 'openLink':
        if (message.url) { await vscode.env.openExternal(vscode.Uri.parse(message.url)); }
        return;
      case 'sendToAgent':
        await this.sendToAgent();
        return;
      case 'showHistory':
        if (message.key) { this.showHistory(message.key); }
        return;
      case 'refresh': {
        const active = this.activeEntry();
        if (active) { await this.explain(active.selection, true); }
        return;
      }
      case 'retry':
        if (this.lastFailure) { await this.explain(this.lastFailure, true); }
        return;
      case 'reveal':
        await this.revealActiveRange();
        return;
      case 'clearHistory':
        this.history = [];
        this.activeKey = undefined;
        this.post({ type: 'render', html: emptyFragment() });
        return;
      default:
        return;
    }
  }

  // --- the one entry point ---------------------------------------------------

  /**
   * Explain a selection. Served from history when the same bytes at the same lines
   * were already explained, which makes re-visiting a range instant rather than
   * re-paying query build + rerank + synthesis.
   */
  async explain(selection: Selection, force = false): Promise<void> {
    await this.reveal();
    const key = keyFor(selection);

    if (!force) {
      const hit = this.history.find((entry) => entry.key === key);
      if (hit) {
        hit.at = Date.now();
        this.activeKey = key;
        this.renderEntry(hit, true);
        return;
      }
    }

    this.lastFailure = undefined;
    this.showLoading(selection);

    try {
      const response = await postContext(this.serviceUrl(), selection);
      this.stopLoading();
      const entry: Entry = { key, selection, response, at: Date.now() };
      this.remember(entry);
      this.activeKey = key;
      this.renderEntry(entry, false);
    } catch (err) {
      this.stopLoading();
      this.lastFailure = selection;
      this.post({
        type: 'render',
        html: this.chrome(errorFragment(this.serviceUrl(), String(err))),
      });
    }
  }

  private remember(entry: Entry): void {
    this.history = [entry, ...this.history.filter((e) => e.key !== entry.key)]
      .slice(0, MAX_HISTORY);
  }

  private activeEntry(): Entry | undefined {
    return this.history.find((entry) => entry.key === this.activeKey);
  }

  private showHistory(key: string): void {
    const entry = this.history.find((e) => e.key === key);
    if (!entry) { return; }
    this.activeKey = key;
    this.renderEntry(entry, true);
  }

  private showLoading(selection: Selection): void {
    this.stopLoading();
    // Painted once. Only the stage text is swapped afterwards -- reassigning
    // webview.html on an interval would rebuild the whole document every tick.
    this.post({ type: 'render', html: this.chrome(loadingFragment(selection)) });
    let stage = 0;
    this.loadingTimer = setInterval(() => {
      stage = (stage + 1) % LOADING_STAGES.length;
      this.post({ type: 'stage', text: LOADING_STAGES[stage] });
    }, STAGE_INTERVAL_MS);
  }

  private stopLoading(): void {
    if (this.loadingTimer) {
      clearInterval(this.loadingTimer);
      this.loadingTimer = undefined;
    }
  }

  private renderEntry(entry: Entry, cached: boolean): void {
    this.post({
      type: 'render',
      html: this.chrome(resultFragment(entry, cached)),
      preserveScroll: false,
    });
  }

  /** The history strip is part of every paint, so it survives loading and errors. */
  private chrome(body: string): string {
    return historyStrip(this.history, this.activeKey) + body;
  }

  private async revealActiveRange(): Promise<void> {
    const entry = this.activeEntry();
    if (!entry || !entry.selection.repo_root) { return; }
    const uri = vscode.Uri.joinPath(
      vscode.Uri.file(entry.selection.repo_root), entry.selection.file_path,
    );
    try {
      const document = await vscode.workspace.openTextDocument(uri);
      const editor = await vscode.window.showTextDocument(document, { preview: false });
      const range = new vscode.Range(
        entry.selection.line_start - 1, 0,
        entry.selection.line_end - 1, Number.MAX_SAFE_INTEGER,
      );
      editor.selection = new vscode.Selection(range.start, range.end);
      editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
    } catch {
      vscode.window.showWarningMessage(
        `Provenance: could not open ${entry.selection.file_path}.`,
      );
    }
  }

  /** Copy the findings as markdown and append them to `.provenance/context.md` in
   *  the workspace, which is where a coding agent is pointed to pick them up. */
  private async sendToAgent(): Promise<void> {
    const entry = this.activeEntry();
    if (!entry) { return; }
    const markdown = toMarkdown(entry.response, entry.selection);

    await vscode.env.clipboard.writeText(markdown);

    const folder = vscode.workspace.workspaceFolders?.[0];
    if (!folder) {
      vscode.window.showInformationMessage('Provenance: copied to clipboard.');
      return;
    }

    const target = vscode.Uri.joinPath(folder.uri, '.provenance', 'context.md');
    try {
      await vscode.workspace.fs.createDirectory(vscode.Uri.joinPath(folder.uri, '.provenance'));
      let existing = '';
      try {
        existing = new TextDecoder().decode(await vscode.workspace.fs.readFile(target));
      } catch {
        existing = '';
      }
      const separator = existing.trim().length > 0 ? '\n\n---\n\n' : '';
      await vscode.workspace.fs.writeFile(
        target,
        new TextEncoder().encode(existing + separator + markdown),
      );
      vscode.window.showInformationMessage(
        'Provenance: copied to clipboard and appended to .provenance/context.md',
      );
    } catch (err) {
      vscode.window.showWarningMessage(
        `Provenance: copied to clipboard, but could not write .provenance/context.md (${err})`,
      );
    }
  }

  dispose(): void {
    this.stopLoading();
  }

  // --- the shell: painted once, then fed fragments over postMessage ----------

  private shell(webview: vscode.Webview): string {
    const csp = webview.cspSource;
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src ${csp} 'unsafe-inline'; script-src 'unsafe-inline';" />
<style>
${STYLES}
</style>
</head>
<body>
<div id="app"></div>
<script>
${CLIENT_SCRIPT}
</script>
</body>
</html>`;
  }
}

// --- fragments ---------------------------------------------------------------

function emptyFragment(): string {
  return `
    <div class="placeholder">
      <p>Select code in the editor and press <kbd>cmd/ctrl</kbd>+<kbd>alt</kbd>+<kbd>W</kbd>.</p>
      <p class="muted">Provenance recovers the commit, the pull request, the discussions,
      the ticket and the incident behind those lines.</p>
    </div>`;
}

function loadingFragment(selection: Selection): string {
  return `
    <div class="loading">
      <div class="spinner" aria-hidden="true"></div>
      <p class="stage">Working…</p>
      <p class="muted" id="stage-text">${escapeHtml(LOADING_STAGES[0])}</p>
      <p class="muted">${escapeHtml(shortLabel(selection))}</p>
    </div>`;
}

function errorFragment(serviceUrl: string, detail: string): string {
  return `
    <div class="notice error">
      <p>Could not reach the Provenance service at ${escapeHtml(serviceUrl)}.</p>
      <p class="muted diagnostic">${escapeHtml(detail)}</p>
      <p class="muted">Start it with <code>make serve</code>, or change
         <code>provenance.serviceUrl</code> in settings.</p>
      <p><button id="retry">Retry</button></p>
    </div>`;
}

function historyStrip(history: Entry[], activeKey: string | undefined): string {
  if (history.length === 0) { return ''; }
  const chips = history.map((entry) => {
    const active = entry.key === activeKey ? ' active' : '';
    return `<button class="chip${active}" data-history="${escapeHtml(entry.key)}"
             title="${escapeHtml(entry.selection.file_path)}:${entry.selection.line_start}-${entry.selection.line_end}"
            >${escapeHtml(shortLabel(entry.selection))}</button>`;
  }).join('');
  return `
    <nav class="history" aria-label="Recent explanations">
      <span class="history-label">Recent</span>
      ${chips}
      <button class="chip ghost" id="clear-history" title="Forget these">clear</button>
    </nav>`;
}

function resultFragment(entry: Entry, cached: boolean): string {
  const { response, selection } = entry;
  const parts: string[] = [];

  parts.push(`
    <header>
      <h1>
        <button class="linklike" id="reveal"
                title="Reveal these lines in the editor">${escapeHtml(selection.file_path)}<span class="muted">:${selection.line_start}-${selection.line_end}</span></button>
      </h1>
      ${blameLine(response.blame)}
      <div class="head-actions">
        ${cached ? '<span class="badge">cached</span>' : ''}
        <button class="linklike" id="refresh" title="Re-run the pipeline for this range">refresh</button>
      </div>
    </header>`);

  if (response.message) {
    parts.push(`<div class="notice"><p>${escapeHtml(response.message)}</p></div>`);
  }
  if (response.error) {
    parts.push(`<p class="muted diagnostic">${escapeHtml(response.error)}</p>`);
  }

  if (response.synthesis) {
    parts.push(`
      <section>
        <h2>Why this code is the way it is</h2>
        <p class="synthesis">${linkCitations(response.synthesis, response.results.length)}</p>
      </section>`);
  }

  if (response.conflicts.length > 0) {
    const items = response.conflicts.map((c) => {
      const text = c.kind === 'supersede'
        ? `[${c.b}] was superseded by [${c.a}] — treat [${c.a}] as current.`
        : `[${c.a}] and [${c.b}] disagree — the merged PR is ground truth.`;
      return `<li>${linkCitations(text, response.results.length)}</li>`;
    }).join('');
    parts.push(`<section class="conflicts"><h2>Disagreements</h2><ul>${items}</ul></section>`);
  }

  if (response.results.length > 0) {
    parts.push(`
      <section>
        <h2>Evidence</h2>
        ${response.results.map((r, i) => resultCard(r, i + 1)).join('')}
      </section>`);
  }

  // Collapsed by default: in a sidebar the graph is a deep-dive, not the headline.
  parts.push(`
    <section class="graph-section">
      <details id="graph-details">
        <summary><h2>Provenance graph</h2></summary>
        ${renderGraph(response.graph)}
      </details>
    </section>`);

  const timings = Object.entries(response.timing_ms)
    .map(([k, v]) => `${escapeHtml(k)} ${v}ms`).join(' · ');

  parts.push(`
    <footer>
      <button id="send-to-agent">Send to agent</button>
      ${timings ? `<details class="more"><summary>Timing</summary><p class="muted">${timings}</p></details>` : ''}
    </footer>`);

  return parts.join('');
}

function blameLine(blame: ContextResponse['blame']): string {
  if (blame.dominant_sha) {
    const authors = blame.authors.join(', ') || 'unknown';
    const prs = blame.pr_numbers.map((p) => `#${p}`).join(', ');
    return `<p class="muted">${escapeHtml(blame.dominant_sha)} · ${escapeHtml(blame.commit_date ?? '')}
            · ${escapeHtml(authors)}${prs ? ` · ${escapeHtml(prs)}` : ''}</p>`;
  }
  if (blame.uncommitted) {
    return '<p class="muted">These lines are not committed yet — no commit anchor.</p>';
  }
  return '<p class="muted">No git history for this range.</p>';
}

/** Backticked identifiers are common in these summaries; render them as code.
 *  Runs on already-escaped text, and escapeHtml leaves backticks alone. */
function inlineCode(escaped: string): string {
  return escaped.replace(/`([^`]+)`/g, '<code>$1</code>');
}

function truncate(value: string, limit: number): string {
  const flat = value.replace(/\s+/g, ' ').trim();
  return flat.length <= limit ? flat : `${flat.slice(0, limit - 1)}…`;
}

function resultCard(result: Result, index: number): string {
  const isExact = result.match_type === 'exact';
  const people = result.participants.join(', ');
  return `
    <details class="card ${isExact ? 'exact' : ''}" id="result-${index}">
      <summary>
        <span class="card-head">
          <strong>[${index}]</strong>
          <span class="tag ${isExact ? 'exact' : ''}">${isExact ? 'exact match' : 'semantic'}</span>
          <span>#${escapeHtml(result.channel_name)}</span>
          <span class="muted">${escapeHtml(result.date)}</span>
        </span>
        <span class="preview">${escapeHtml(truncate(result.summary, 90))}</span>
      </summary>
      <p>${inlineCode(escapeHtml(result.summary))}</p>
      ${result.why ? `<p class="why">${inlineCode(escapeHtml(result.why))}</p>` : ''}
      <p class="muted">
        ${people ? `${escapeHtml(people)} · ` : ''}
        <a href="${escapeHtml(result.permalink)}" data-external>open in Slack</a>
      </p>
    </details>`;
}

export function toMarkdown(response: ContextResponse, selection: Selection): string {
  const lines: string[] = [
    `## Provenance: ${selection.file_path}:${selection.line_start}-${selection.line_end}`,
    '',
  ];

  const { blame } = response;
  if (blame.dominant_sha) {
    const prs = blame.pr_numbers.map((p) => `PR #${p}`).join(', ');
    lines.push(
      `**Origin**: commit \`${blame.dominant_sha}\` by ${blame.authors.join(', ') || 'unknown'}` +
      `${blame.commit_date ? ` on ${blame.commit_date}` : ''}${prs ? ` — ${prs}` : ''}`,
      '',
    );
  }

  if (response.synthesis) {
    lines.push(response.synthesis, '');
  }
  if (response.message) {
    lines.push(response.message, '');
  }

  if (response.results.length > 0) {
    lines.push('### Evidence', '');
    response.results.forEach((r, i) => {
      lines.push(
        `${i + 1}. **#${r.channel_name}** — ${r.date} (${r.match_type})`,
        `   ${r.summary}`,
        r.why ? `   _${r.why}_` : '',
        `   ${r.permalink}`,
        '',
      );
    });
  }

  for (const c of response.conflicts) {
    lines.push(
      c.kind === 'supersede'
        ? `> [${c.b}] was superseded by [${c.a}] — treat [${c.a}] as current.`
        : `> [${c.a}] and [${c.b}] disagree — the merged PR is ground truth.`,
    );
  }

  lines.push(
    '',
    'Treat this evidence as constraints: a change that ignores it is likely a regression.',
  );
  return lines.filter((l) => l !== undefined).join('\n');
}

// --- static assets -----------------------------------------------------------

const STYLES = `
  :root { color-scheme: light dark; }
  body {
    font-family: var(--vscode-font-family); color: var(--vscode-foreground);
    background: var(--vscode-sideBar-background, var(--vscode-editor-background));
    padding: 10px 12px 28px; line-height: 1.5; font-size: 0.9rem;
    overflow-wrap: anywhere;
  }
  h1 { font-size: 0.98rem; margin: 0 0 4px; font-weight: 600; word-break: break-all; }
  h2 { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em;
       opacity: 0.65; margin: 18px 0 8px; font-weight: 600; display: inline; }
  header { border-bottom: 1px solid var(--vscode-panel-border); padding-bottom: 10px; }
  .head-actions { display: flex; gap: 8px; align-items: center; margin-top: 4px; }
  .muted { opacity: 0.65; font-size: 0.82rem; }
  .diagnostic { font-family: var(--vscode-editor-font-family); word-break: break-all; }
  .synthesis { font-size: 0.93rem; }
  .citation { color: var(--vscode-textLink-foreground); text-decoration: none; font-weight: 600; }
  .notice { background: var(--vscode-textBlockQuote-background);
            border-left: 3px solid var(--vscode-panel-border); padding: 8px 12px; margin: 14px 0; }
  .notice.error { border-left-color: var(--vscode-errorForeground); }
  .card { border: 1px solid var(--vscode-panel-border); border-radius: 6px;
          padding: 9px 11px; margin-bottom: 9px; overflow-wrap: anywhere; }
  .card.exact { border-left: 3px solid var(--vscode-charts-blue, #4a9eff); }
  .card.flash { animation: flash 1.1s ease; }
  @keyframes flash { from { background: var(--vscode-editor-findMatchHighlightBackground, rgba(255,214,0,0.35)); } }
  .card > summary { cursor: pointer; user-select: none; }
  .card > summary:hover .preview { opacity: 0.95; }
  .card > p { margin: 8px 0 0; }
  .preview { display: block; margin-top: 3px; opacity: 0.7; font-size: 0.83rem; }
  .card[open] .preview { display: none; }
  .card-head { display: inline-flex; gap: 7px; align-items: baseline; flex-wrap: wrap; }
  code { font-family: var(--vscode-editor-font-family); font-size: 0.82rem;
         background: var(--vscode-textCodeBlock-background, rgba(127, 127, 127, 0.18));
         border-radius: 3px; padding: 0 3px; overflow-wrap: anywhere; }
  .tag { font-size: 0.65rem; letter-spacing: 0.06em; text-transform: uppercase;
         border-radius: 3px; padding: 1px 5px; border: 1px solid var(--vscode-panel-border); }
  .tag.exact { border-color: var(--vscode-charts-blue, #4a9eff); }
  .why { font-style: italic; opacity: 0.8; font-size: 0.84rem; margin: 4px 0 0; }
  .more { margin-top: 6px; }
  .more summary { cursor: pointer; font-size: 0.78rem; opacity: 0.7; user-select: none; }
  .more summary:hover { opacity: 1; }
  .more[open] summary { margin-bottom: 4px; }
  .conflicts ul { margin: 0; padding-left: 18px; }
  a { color: var(--vscode-textLink-foreground); }
  kbd { font-family: var(--vscode-editor-font-family); font-size: 0.78rem;
        border: 1px solid var(--vscode-panel-border); border-radius: 3px; padding: 0 4px; }
  footer { margin-top: 22px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  button { background: var(--vscode-button-background); color: var(--vscode-button-foreground);
           border: none; border-radius: 3px; padding: 5px 11px; cursor: pointer;
           font-family: inherit; font-size: 0.83rem; }
  button:hover { background: var(--vscode-button-hoverBackground); }
  .linklike { background: none; color: var(--vscode-textLink-foreground); padding: 0;
              font-size: inherit; text-align: left; }
  .linklike:hover { background: none; text-decoration: underline; }
  .badge { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.06em;
           border: 1px solid var(--vscode-panel-border); border-radius: 3px;
           padding: 1px 5px; opacity: 0.7; }
  .placeholder { padding: 18px 2px; }

  .history { display: flex; flex-wrap: wrap; gap: 4px; align-items: center;
             padding-bottom: 9px; margin-bottom: 9px;
             border-bottom: 1px solid var(--vscode-panel-border); }
  .history-label { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.06em;
                   opacity: 0.5; margin-right: 2px; }
  .chip { background: var(--vscode-editorWidget-background); color: var(--vscode-foreground);
          border: 1px solid var(--vscode-panel-border); border-radius: 10px;
          padding: 1px 8px; font-size: 0.73rem; }
  .chip:hover { background: var(--vscode-list-hoverBackground); }
  .chip.active { border-color: var(--vscode-textLink-foreground);
                 color: var(--vscode-textLink-foreground); }
  .chip.ghost { opacity: 0.5; border-style: dashed; }

  .loading { display: flex; flex-direction: column; align-items: center;
             justify-content: center; min-height: 45vh; gap: 8px; text-align: center; }
  .spinner { width: 20px; height: 20px; border-radius: 50%;
             border: 2px solid var(--vscode-panel-border);
             border-top-color: var(--vscode-textLink-foreground);
             animation: spin 0.9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spinner { animation: none; } }
  .stage { margin: 0; font-size: 0.92rem; }

  /* -- Provenance graph: toolbar, pannable/zoomable viewport, legend, details -- */
  .graph-section { margin-bottom: 4px; }
  .graph-section summary { cursor: pointer; margin: 18px 0 4px; user-select: none; }
  .graph-toolbar { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; flex-wrap: wrap; }
  .graph-btn { background: var(--vscode-editorWidget-background);
               color: var(--vscode-foreground); border: 1px solid var(--vscode-panel-border);
               border-radius: 4px; width: 24px; height: 24px; line-height: 1; font-size: 0.9rem;
               cursor: pointer; padding: 0; }
  .graph-btn:hover { background: var(--vscode-list-hoverBackground); }
  #graph-zoom-reset { width: auto; padding: 0 9px; font-size: 0.74rem; }
  .graph-hint { opacity: 0.55; font-size: 0.7rem; }

  .graph-viewport-outer {
    position: relative; overflow: hidden; height: 46vh; min-height: 260px;
    border: 1px solid var(--vscode-panel-border); border-radius: 8px;
    background:
      radial-gradient(var(--vscode-panel-border) 1px, transparent 1px) 0 0 / 22px 22px,
      var(--vscode-editor-background);
    cursor: grab;
  }
  .graph-viewport-outer.grabbing { cursor: grabbing; }
  .graph-svg { transform-origin: 0 0; will-change: transform; user-select: none; }

  .graph-svg .node rect { fill: var(--vscode-editorWidget-background);
                          stroke: var(--vscode-panel-border); stroke-width: 1.2; }
  .graph-svg .node .accent { opacity: 0.9; }
  .graph-svg .node { cursor: pointer; transition: opacity 120ms ease; }
  .graph-svg .node:hover rect:not(.accent), .graph-svg .node:focus rect:not(.accent),
  .graph-svg .node.active rect:not(.accent) { stroke: var(--vscode-focusBorder); stroke-width: 2; }
  .graph-svg .node.dimmed { opacity: 0.28; }
  .graph-svg .node.selected rect:not(.accent) { stroke: var(--vscode-textLink-foreground); stroke-width: 2.5; }

  .graph-svg .n-code .accent { fill: var(--vscode-charts-blue, #4a9eff); }
  .graph-svg .n-commit .accent { fill: var(--vscode-charts-yellow, #cca700); }
  .graph-svg .n-pr .accent { fill: var(--vscode-charts-green, #89d185); }
  .graph-svg .n-slack .accent { fill: var(--vscode-charts-purple, #b180d7); }
  .graph-svg .n-ticket .accent { fill: var(--vscode-charts-orange, #d18616); }
  .graph-svg .n-sentry .accent { fill: var(--vscode-charts-red, #f14c4c); }
  .graph-svg .n-person .accent { fill: var(--vscode-descriptionForeground); }

  .graph-svg .node-icon { font-size: 15px; }
  .graph-svg .node-type { font-size: 9px; fill: var(--vscode-foreground); opacity: 0.6;
                          text-transform: uppercase; letter-spacing: 0.05em; }
  .graph-svg .node-label { font-size: 12.5px; fill: var(--vscode-foreground); font-weight: 600; }
  .graph-svg .node-subtitle { font-size: 10px; fill: var(--vscode-foreground); opacity: 0.55; }

  .graph-svg .edge path { fill: none; stroke: var(--vscode-panel-border); stroke-width: 1.4;
                          transition: opacity 120ms ease, stroke-width 120ms ease; }
  .graph-svg .edge.inferred path { stroke-dasharray: 4 3; stroke: var(--vscode-charts-orange, #d18616); }
  .graph-svg .edge.dimmed path { opacity: 0.15; }
  .graph-svg .edge.active path { stroke: var(--vscode-textLink-foreground); stroke-width: 2.2; opacity: 1; }
  .graph-svg marker path { fill: var(--vscode-panel-border); stroke: none; }
  .graph-svg marker .inferred-head { fill: var(--vscode-charts-orange, #d18616); }
  .graph-svg .edge-label { font-size: 8px; fill: var(--vscode-foreground); opacity: 0.45; text-anchor: middle; }

  .graph-legend { display: flex; flex-wrap: wrap; gap: 5px 9px; margin-top: 8px; }
  .legend-chip { font-size: 0.68rem; opacity: 0.8; display: inline-flex; align-items: center; gap: 4px;
                border-left: 3px solid transparent; padding-left: 5px; }
  .legend-chip.n-code { border-color: var(--vscode-charts-blue, #4a9eff); }
  .legend-chip.n-commit { border-color: var(--vscode-charts-yellow, #cca700); }
  .legend-chip.n-pr { border-color: var(--vscode-charts-green, #89d185); }
  .legend-chip.n-slack { border-color: var(--vscode-charts-purple, #b180d7); }
  .legend-chip.n-ticket { border-color: var(--vscode-charts-orange, #d18616); }
  .legend-chip.n-sentry { border-color: var(--vscode-charts-red, #f14c4c); }
  .legend-chip.n-person { border-color: var(--vscode-descriptionForeground); }
  .legend-chip.legend-edge { border: none; opacity: 0.6; }
  .legend-chip.legend-edge.inferred { color: var(--vscode-charts-orange, #d18616); }

  .node-details { margin-top: 10px; border: 1px solid var(--vscode-panel-border); border-radius: 6px;
                  padding: 9px 11px; font-size: 0.82rem; min-height: 20px; }
  .node-details.empty { opacity: 0.5; font-style: italic; }
  .node-details .nd-title { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .node-details .nd-title strong { font-size: 0.87rem; }
  .node-details .nd-row { display: flex; gap: 6px; margin: 2px 0; }
  .node-details .nd-key { opacity: 0.55; min-width: 84px; }
  .node-details .nd-open { margin-top: 8px; }

  .empty { opacity: 0.6; font-size: 0.85rem; }
`;

// Plain concatenation, no template literals: this string is itself interpolated into
// one, so a `${` here would be evaluated by the extension host instead of the webview.
const CLIENT_SCRIPT = `
  const vscodeApi = acquireVsCodeApi();
  const app = document.getElementById('app');
  let destroyGraph = null;

  window.addEventListener('message', function (event) {
    const message = event.data;
    if (message.type === 'render') {
      const offset = message.preserveScroll ? window.scrollY : 0;
      app.innerHTML = message.html;
      initGraph();
      window.scrollTo(0, offset);
    } else if (message.type === 'stage') {
      const el = document.getElementById('stage-text');
      if (el) { el.textContent = message.text; }
    }
  });

  // One delegated handler: the content below #app is replaced on every render, so
  // per-element listeners would have to be rebound (and would leak) each time.
  document.addEventListener('click', function (event) {
    const target = event.target;
    if (!(target instanceof Element)) { return; }

    const citation = target.closest('.citation');
    if (citation) {
      event.preventDefault();
      const card = document.getElementById(citation.getAttribute('data-target') || '');
      if (card) {
        // The cards ship collapsed; a citation that scrolled to a closed one
        // would look like it had gone nowhere.
        if (card.tagName === 'DETAILS') { card.open = true; }
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        card.classList.remove('flash');
        void card.offsetWidth;
        card.classList.add('flash');
      }
      return;
    }

    const external = target.closest('a[data-external]');
    if (external) {
      event.preventDefault();
      vscodeApi.postMessage({ type: 'openLink', url: external.getAttribute('href') });
      return;
    }

    const chip = target.closest('[data-history]');
    if (chip) {
      vscodeApi.postMessage({ type: 'showHistory', key: chip.getAttribute('data-history') });
      return;
    }

    const button = target.closest('button');
    if (!button) { return; }
    if (button.id === 'send-to-agent') { vscodeApi.postMessage({ type: 'sendToAgent' }); }
    else if (button.id === 'refresh') { vscodeApi.postMessage({ type: 'refresh' }); }
    else if (button.id === 'retry') { vscodeApi.postMessage({ type: 'retry' }); }
    else if (button.id === 'reveal') { vscodeApi.postMessage({ type: 'reveal' }); }
    else if (button.id === 'clear-history') { vscodeApi.postMessage({ type: 'clearHistory' }); }
  });

  // ---- Provenance graph: pan, zoom, hover-to-trace, click-for-details ----
  function initGraph() {
    if (destroyGraph) { destroyGraph(); destroyGraph = null; }

    const outer = document.getElementById('graph-viewport-outer');
    const svg = document.getElementById('graph-svg');
    if (!outer || !svg) { return; }

    const canvasWidth = parseFloat(svg.getAttribute('data-canvas-width') || svg.getAttribute('width') || '0');
    const canvasHeight = parseFloat(svg.getAttribute('data-canvas-height') || svg.getAttribute('height') || '0');

    let scale = 1;
    let tx = 0;
    let ty = 0;
    const MIN_SCALE = 0.25;
    const MAX_SCALE = 2.5;

    function apply() {
      svg.style.transform = 'translate(' + tx + 'px, ' + ty + 'px) scale(' + scale + ')';
    }

    function fit() {
      const rect = outer.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) { return; }
      const pad = 24;
      const fitScale = Math.min(
        (rect.width - pad) / canvasWidth,
        (rect.height - pad) / canvasHeight,
        1
      );
      scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, fitScale || 1));
      tx = (rect.width - canvasWidth * scale) / 2;
      ty = (rect.height - canvasHeight * scale) / 2;
      apply();
    }

    function zoomBy(factor, originX, originY) {
      const rect = outer.getBoundingClientRect();
      const cx = originX !== undefined ? originX - rect.left : rect.width / 2;
      const cy = originY !== undefined ? originY - rect.top : rect.height / 2;
      const next = Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale * factor));
      // Zoom around the cursor (or the center), not the top-left corner.
      tx = cx - ((cx - tx) / scale) * next;
      ty = cy - ((cy - ty) / scale) * next;
      scale = next;
      apply();
    }

    const zoomIn = document.getElementById('graph-zoom-in');
    const zoomOut = document.getElementById('graph-zoom-out');
    const zoomReset = document.getElementById('graph-zoom-reset');
    if (zoomIn) { zoomIn.addEventListener('click', function () { zoomBy(1.25); }); }
    if (zoomOut) { zoomOut.addEventListener('click', function () { zoomBy(0.8); }); }
    if (zoomReset) { zoomReset.addEventListener('click', fit); }

    function onWheel(event) {
      event.preventDefault();
      zoomBy(event.deltaY < 0 ? 1.08 : 0.93, event.clientX, event.clientY);
    }
    outer.addEventListener('wheel', onWheel, { passive: false });

    let dragging = false;
    let lastX = 0;
    let lastY = 0;
    function onDown(event) {
      dragging = true;
      lastX = event.clientX;
      lastY = event.clientY;
      outer.classList.add('grabbing');
    }
    function onMove(event) {
      if (!dragging) { return; }
      tx += event.clientX - lastX;
      ty += event.clientY - lastY;
      lastX = event.clientX;
      lastY = event.clientY;
      apply();
    }
    function onUp() {
      dragging = false;
      outer.classList.remove('grabbing');
    }
    outer.addEventListener('mousedown', onDown);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    window.addEventListener('resize', fit);

    // Hover a node -> highlight it, its directly connected nodes, and the edges
    // between them; dim everything else so the evidence chain reads at a glance.
    const nodeEls = Array.prototype.slice.call(svg.querySelectorAll('.node'));
    const edgeEls = Array.prototype.slice.call(svg.querySelectorAll('.edge'));

    function setTrace(activeId) {
      if (!activeId) {
        nodeEls.forEach(function (n) { n.classList.remove('active', 'dimmed'); });
        edgeEls.forEach(function (e) { e.classList.remove('active', 'dimmed'); });
        return;
      }
      const connected = new Set([activeId]);
      edgeEls.forEach(function (e) {
        const source = e.getAttribute('data-source');
        const target = e.getAttribute('data-target');
        const touches = source === activeId || target === activeId;
        e.classList.toggle('active', touches);
        e.classList.toggle('dimmed', !touches);
        if (touches) {
          if (source) { connected.add(source); }
          if (target) { connected.add(target); }
        }
      });
      nodeEls.forEach(function (n) {
        const id = n.getAttribute('data-node-id');
        const isConnected = id !== null && connected.has(id);
        n.classList.toggle('active', isConnected);
        n.classList.toggle('dimmed', !isConnected);
      });
    }

    const details = document.getElementById('node-details');
    function escapeText(value) {
      const div = document.createElement('div');
      div.textContent = String(value);
      return div.innerHTML;
    }
    function renderDetails(node) {
      if (!details) { return; }
      nodeEls.forEach(function (n) {
        n.classList.toggle('selected', n.getAttribute('data-node-id') === node.id);
      });
      details.classList.remove('empty');
      const data = node.data || {};
      const rows = Object.keys(data)
        .filter(function (key) {
          return key !== 'permalink' && data[key] !== undefined && data[key] !== null && data[key] !== '';
        })
        .map(function (key) {
          const value = Array.isArray(data[key]) ? data[key].join(', ') : data[key];
          return '<div class="nd-row"><span class="nd-key">' + escapeText(key) + '</span><span>' + escapeText(value) + '</span></div>';
        }).join('');
      const permalink = typeof data.permalink === 'string' ? data.permalink : '';
      details.innerHTML =
        '<div class="nd-title"><strong>' + escapeText(node.type) + ': ' + escapeText(node.label) + '</strong></div>' +
        (rows || '<div class="nd-row muted">No further detail resolved for this node.</div>') +
        (permalink ? '<div class="nd-open"><button id="nd-open-btn">Open externally ↗</button></div>' : '');
      const openBtn = document.getElementById('nd-open-btn');
      if (openBtn) {
        openBtn.addEventListener('click', function () {
          vscodeApi.postMessage({ type: 'openLink', url: permalink });
        });
      }
    }

    nodeEls.forEach(function (n) {
      n.addEventListener('mouseenter', function () { setTrace(n.getAttribute('data-node-id')); });
      n.addEventListener('mouseleave', function () { setTrace(null); });
      n.addEventListener('focus', function () { setTrace(n.getAttribute('data-node-id')); });
      n.addEventListener('blur', function () { setTrace(null); });
      n.addEventListener('click', function () {
        try {
          renderDetails(JSON.parse(n.getAttribute('data-node-json') || '{}'));
        } catch (err) { /* malformed payload -- leave the details panel untouched */ }
      });
    });

    destroyGraph = function () {
      outer.removeEventListener('wheel', onWheel);
      outer.removeEventListener('mousedown', onDown);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      window.removeEventListener('resize', fit);
    };

    // The graph ships collapsed, so it has no measurable box until it is opened.
    const wrapper = document.getElementById('graph-details');
    if (wrapper) {
      wrapper.addEventListener('toggle', function () {
        if (wrapper.open) { requestAnimationFrame(fit); }
      });
    }
    if (outer.offsetParent !== null) { requestAnimationFrame(fit); }
  }

  vscodeApi.postMessage({ type: 'ready' });
`;
