import { createHash } from 'crypto';
import * as vscode from 'vscode';
import { getIngestStatus, postContext, postIngestSync } from './api';
import { renderGraph } from './graph';
import { ContextResponse, IngestStatus, Result, Selection } from './types';

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

  /** A sync is one-at-a-time in the service too; this keeps the panel from asking. */
  private ingestBusy = false;

  private history: Entry[] = [];
  private activeKey: string | undefined;
  private lastFailure: Selection | undefined;
  private timelinePanel: vscode.WebviewPanel | undefined;

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
    // A webview view is rebuilt from the shell when it is re-shown, so the bar comes
    // back empty; ask the service where the index stands again rather than caching it.
    void this.refreshIngestStatus();

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
      case 'backfill':
        await this.backfill();
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
      case 'openTimeline':
        this.openTimeline();
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

  /** The sidebar is ~340px; the graph outgrows it fast. Same renderer, more room. */
  openTimeline(): void {
    const entry = this.activeEntry();
    if (!entry) {
      void vscode.window.showInformationMessage('Provenance: explain a selection first.');
      return;
    }

    const fresh = !this.timelinePanel;
    if (!this.timelinePanel) {
      this.timelinePanel = vscode.window.createWebviewPanel(
        'provenanceTimeline',
        'Provenance timeline',
        vscode.ViewColumn.Active,
        { enableScripts: true, retainContextWhenHidden: true },
      );
      this.timelinePanel.onDidDispose(() => { this.timelinePanel = undefined; });
      // Same shell and same client script as the sidebar, so pan, zoom,
      // hover-to-trace and click-for-details all behave identically. The panel
      // only has to answer 'ready' with a fragment and open external links.
      this.timelinePanel.webview.onDidReceiveMessage((m: { type: string; url?: string }) => {
        if (m.type === 'ready') {
          const active = this.activeEntry();
          if (active) {
            void this.timelinePanel?.webview.postMessage({
              type: 'render', html: timelineFragment(active),
            });
          }
          return;
        }
        if (m.type === 'openLink' && m.url) {
          void vscode.env.openExternal(vscode.Uri.parse(m.url));
        }
      });
    }

    const { selection } = entry;
    this.timelinePanel.title = `Timeline: ${selection.file_path}:${selection.line_start}-${selection.line_end}`;
    if (fresh) {
      this.timelinePanel.webview.html = this.shell(this.timelinePanel.webview);
    } else {
      void this.timelinePanel.webview.postMessage({ type: 'render', html: timelineFragment(entry) });
    }
    this.timelinePanel.reveal(vscode.ViewColumn.Active, false);
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

  // --- indexing --------------------------------------------------------------

  private postStatusBar(
    note: string,
    opts: { busy?: boolean; label?: string; isError?: boolean; title?: string } = {},
  ): void {
    this.post({
      type: 'statusbar',
      note,
      busy: opts.busy ?? this.ingestBusy,
      label: opts.label ?? 'Backfill',
      isError: opts.isError ?? false,
      title: opts.title ?? '',
    });
  }

  /** Read the coverage line off the service. Never blocks a press; never hits Slack. */
  private async refreshIngestStatus(): Promise<void> {
    if (this.ingestBusy) { return; }
    try {
      const status = await getIngestStatus(this.serviceUrl());
      this.postStatusBar(coverageNote(status), { title: channelDetail(status.channels) });
    } catch {
      // History is served from memory, so the panel is useful with the service down.
      // Painting an error across the bar would overstate what is broken.
      this.postStatusBar('');
    }
  }

  /**
   * Index everything posted since the last sync.
   *
   * The window is the service's to decide, not the panel's: it holds the checkpoint,
   * and a timestamp chosen here would drift from the one `python -m provenance.ingest`
   * writes. The panel only says "catch up" and reports what came back.
   */
  private async backfill(): Promise<void> {
    if (this.ingestBusy) { return; }
    this.ingestBusy = true;
    this.postStatusBar('Reading Slack and indexing what is new…',
                       { busy: true, label: 'Backfilling…' });
    try {
      const result = await postIngestSync(this.serviceUrl());
      this.ingestBusy = false;
      const through = result.covered_through ? ` · through ${formatTs(result.covered_through)}` : '';
      this.postStatusBar(
        result.new_messages === 0
          ? `Already up to date${through}`
          : `Indexed ${plural(result.indexed, 'conversation')} from `
            + `${plural(result.new_messages, 'new message')}${through}`,
        { title: result.log.join('\n') },
      );
    } catch (err) {
      this.ingestBusy = false;
      const detail = err instanceof Error ? err.message : String(err);
      // The service's refusals are paragraphs -- a missing scope and how to add it, a
      // corpus mismatch and how to resolve it. The bar is one line, so it says that it
      // failed and the notification carries the instructions.
      this.postStatusBar('Backfill failed', { isError: true, title: detail });
      vscode.window.showErrorMessage(`Provenance backfill: ${detail}`);
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
<!-- Outside #app on purpose: fragments replace that whole subtree on every render,
     and indexing is not a property of the selection being explained. The bar is the
     one control that is always available, including before the first query. -->
<div id="statusbar">
  <span class="sb-note" id="sb-note">&nbsp;</span>
  <button id="backfill" title="Index Slack messages posted since the last sync">Backfill</button>
</div>
<script>
${CLIENT_SCRIPT}
</script>
</body>
</html>`;
  }
}

// --- the status bar ----------------------------------------------------------

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

/** Slack timestamps are epoch seconds; the bar shows them in the reader's timezone. */
function formatTs(ts: number): string {
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

/**
 * The scope suffix: how many channels ingest is pointed at.
 *
 * Worth the pixels because a narrow `SLACK_CHANNEL_IDS` is invisible otherwise --
 * backfill happily reports success having read one channel of fourteen, and the only
 * symptom is a graph with no Slack in it.
 */
function scopeNote(scope: string | number | undefined): string {
  if (scope === undefined) { return ''; }
  if (scope === '*') { return ' · all channels'; }
  const count = Number(scope);
  return Number.isFinite(count) ? ` · ${plural(count, 'channel')}` : '';
}

function coverageNote(status: IngestStatus): string {
  const scope = scopeNote(status.scope);
  if (status.never_run) {
    return `Nothing indexed yet · Backfill reads the whole history${scope}`;
  }
  if (status.covered_through === null) { return ''; }
  const docs = status.docs === undefined ? '' : ` · ${plural(status.docs, 'conversation')}`;
  return `Indexed through ${formatTs(status.covered_through)}${docs}${scope}`;
}

/**
 * The per-channel detail, for the bar's tooltip.
 *
 * The bar itself shows the *oldest* channel's timestamp, because that is the point
 * the whole index is genuinely caught up to. Which channel is lagging only matters
 * once someone asks, so it lives in the hover.
 */
function channelDetail(channels: Record<string, number>): string {
  const names = Object.keys(channels).sort();
  if (names.length === 0) { return ''; }
  return names.map((name) => `#${name} — ${formatTs(channels[name])}`).join('\n');
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

/** The expanded tab: the timeline is the content, not a section inside a report. */
function timelineFragment(entry: Entry): string {
  const { response, selection } = entry;
  return `
    <header>
      <h1>${escapeHtml(selection.file_path)}:${selection.line_start}-${selection.line_end}</h1>
      ${blameLine(response.blame)}
    </header>
    <section class="graph-section fullscreen">
      ${renderGraph(response.graph, { fullscreen: true })}
    </section>`;
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

  // Open by default and above the evidence list: the timeline is the map, and the
  // evidence cards below are what its Slack nodes link into.
  parts.push(`
    <section class="graph-section">
      <details id="graph-details" open>
        <summary><h2>Provenance timeline</h2></summary>
        ${renderGraph(response.graph)}
      </details>
    </section>`);

  if (response.results.length > 0) {
    parts.push(`
      <section>
        <h2>Evidence</h2>
        ${response.results.map((r, i) => resultCard(r, i + 1)).join('')}
      </section>`);
  }

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
    /* Leave room for the fixed status bar so the last card is never under it. */
    padding-bottom: 46px;
  }
  /* Bottom-right, mirroring "Send to agent" at the bottom left of the report. */
  #statusbar {
    position: fixed; left: 0; right: 0; bottom: 0;
    display: flex; align-items: center; gap: 8px;
    padding: 6px 12px;
    background: var(--vscode-sideBar-background, var(--vscode-editor-background));
    border-top: 1px solid var(--vscode-panel-border);
  }
  .sb-note { flex: 1; min-width: 0; font-size: 0.72rem; opacity: 0.6;
             white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sb-note.error { color: var(--vscode-errorForeground); opacity: 0.9;
                   white-space: normal; }
  #backfill[disabled] { opacity: 0.55; cursor: default; }
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

  /* -- Provenance timeline: toolbar, pannable/zoomable viewport, legend, details -- */
  .graph-section { margin-bottom: 4px; }
  .graph-section summary { cursor: pointer; margin: 18px 0 6px; user-select: none; }
  .graph-toolbar { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; flex-wrap: wrap; }
  .graph-btn { background: var(--vscode-editorWidget-background);
               color: var(--vscode-foreground); border: 1px solid var(--vscode-panel-border);
               border-radius: 4px; width: 24px; height: 24px; line-height: 1; font-size: 0.9rem;
               cursor: pointer; padding: 0; }
  .graph-btn:hover { background: var(--vscode-list-hoverBackground); }
  #graph-zoom-reset { width: auto; padding: 0 9px; font-size: 0.74rem; }
  .graph-hint { opacity: 0.55; font-size: 0.7rem; }

  .graph-viewport-outer {
    position: relative; overflow: hidden; height: 52vh; min-height: 300px;
    border: 1px solid var(--vscode-panel-border); border-radius: 8px;
    background: var(--vscode-editor-background);
    cursor: grab;
  }
  .graph-viewport-outer.grabbing { cursor: grabbing; }
  /* The rail usually runs past the fold; fade the bottom edge so that reads as
     "there is more below" rather than as the end of the timeline. */
  .graph-viewport-outer::after {
    content: ''; position: absolute; left: 0; right: 0; bottom: 0; height: 26px;
    background: linear-gradient(transparent, var(--vscode-editor-background));
    pointer-events: none; border-radius: 0 0 8px 8px;
  }
  .graph-svg { transform-origin: 0 0; will-change: transform; user-select: none; }

  /* The rail is the timeline itself: one unbroken spine every card hangs off. */
  .graph-svg .rail { stroke: var(--vscode-foreground); stroke-opacity: 0.3; stroke-width: 2; fill: none; }
  .graph-svg .rail-head { stroke-dasharray: 3 4; stroke-opacity: 0.22; }
  .graph-svg .stub { stroke: var(--vscode-foreground); stroke-opacity: 0.3; stroke-width: 1.4; }
  .graph-svg .dot { stroke: var(--vscode-editor-background); stroke-width: 2; }

  .graph-svg .node .card { fill: var(--vscode-editorWidget-background);
                          stroke: var(--vscode-panel-border); stroke-width: 1.2; }
  .graph-svg .node .accent { opacity: 0.95; stroke: none; }
  .graph-svg .node { cursor: pointer; transition: opacity 120ms ease; }
  .graph-svg .node:hover .card, .graph-svg .node:focus .card,
  .graph-svg .node.active .card, .graph-svg .node.selected .card {
    stroke: var(--vscode-focusBorder); stroke-width: 2;
  }
  .graph-svg .node.dimmed, .graph-svg .chip-node.dimmed { opacity: 0.25; }
  .graph-svg .node.linkable:hover .node-label { text-decoration: underline; }

  /* The card's action, shown only for the card the pointer is actually on.
     Deliberately not '.active': hovering one card marks everything it connects to
     active, and an Open button on four cards at once says nothing about which one
     is about to open. Not '.selected' either -- that outlives the pointer.
     ':focus-visible' rather than ':focus' is the same rule for the keyboard: it
     follows the tab ring, where ':focus' would also latch on to the last card
     clicked and leave its button showing under the mouse's nose.
     It is untouchable while invisible, so the corner of a card that is not being
     hovered is still the card. */
  .graph-svg .node-open { opacity: 0; pointer-events: none; transition: opacity 100ms ease; }
  .graph-svg .node:hover .node-open, .graph-svg .node:focus-visible .node-open {
    opacity: 1; pointer-events: all;
  }
  .graph-svg .node-open .open-bg { fill: var(--vscode-button-secondaryBackground, #3a3d41);
                                   stroke: var(--vscode-panel-border); stroke-width: 1; }
  .graph-svg .node-open text { font-size: 9px; font-weight: 600; letter-spacing: 0.02em;
                               text-anchor: middle; dominant-baseline: central;
                               fill: var(--vscode-button-secondaryForeground, #cccccc); }
  .graph-svg .node-open:hover .open-bg { fill: var(--vscode-button-background, #0078d4);
                                         stroke: var(--vscode-button-background, #0078d4); }
  .graph-svg .node-open:hover text { fill: var(--vscode-button-foreground, #ffffff); }

  .graph-svg .n-code .accent, .graph-svg .n-code .node-glyph, .graph-svg circle.n-code { fill: var(--vscode-charts-blue, #4a9eff); }
  .graph-svg .n-commit .accent, .graph-svg .n-commit .node-glyph, .graph-svg circle.n-commit { fill: var(--vscode-charts-yellow, #cca700); }
  .graph-svg .n-pr .accent, .graph-svg .n-pr .node-glyph, .graph-svg circle.n-pr { fill: var(--vscode-charts-green, #89d185); }
  .graph-svg .n-slack .accent, .graph-svg .n-slack .node-glyph, .graph-svg circle.n-slack { fill: var(--vscode-charts-purple, #b180d7); }
  .graph-svg .n-ticket .accent, .graph-svg .n-ticket .node-glyph, .graph-svg circle.n-ticket { fill: var(--vscode-charts-orange, #d18616); }
  .graph-svg .n-sentry .accent, .graph-svg .n-sentry .node-glyph, .graph-svg circle.n-sentry { fill: var(--vscode-charts-red, #f14c4c); }
  .graph-svg .n-person .accent, .graph-svg .n-person .node-glyph, .graph-svg circle.n-person { fill: var(--vscode-descriptionForeground); }

  .graph-svg .node-icon { font-size: 13px; }
  /* The mark is scaled into place by a transform, so it must not also be stroked.
     Subpaths that carry their own brand colour set it as a fill attribute on the
     path, which outranks the accent colour they would otherwise inherit here. */
  .graph-svg .node-glyph { stroke: none; }
  .graph-svg .node-type { font-size: 8.5px; fill: var(--vscode-foreground); opacity: 0.55;
                          text-transform: uppercase; letter-spacing: 0.06em; }
  .graph-svg .node-date { font-size: 8.5px; fill: var(--vscode-foreground); opacity: 0.5;
                          text-anchor: end; font-family: var(--vscode-editor-font-family); }
  .graph-svg .node-label { font-size: 12px; fill: var(--vscode-foreground); font-weight: 600; }
  .graph-svg .node-subtitle { font-size: 9.5px; fill: var(--vscode-foreground); opacity: 0.6; }

  /* People and tickets ride inside their event's card, not as loose boxes. */
  .graph-svg .chip-node { cursor: pointer; transition: opacity 120ms ease; }
  .graph-svg .chip-node rect { fill: none; stroke: var(--vscode-panel-border); stroke-width: 1; }
  .graph-svg .chip-node text { font-size: 9px; fill: var(--vscode-foreground); opacity: 0.8; }
  .graph-svg .chip-node:hover rect, .graph-svg .chip-node:focus rect { stroke: var(--vscode-focusBorder); }
  .graph-svg .chip-more { font-size: 9px; fill: var(--vscode-foreground); opacity: 0.5; }

  /* Edges carry real structure, so they are legible at rest rather than hinted:
     the old 0.5 opacity on panel-border grey read as "no edge here" and made a
     fully connected graph look like scattered islands. Lanes (graph.ts) keep
     them from overlapping, so they can afford to be seen.

     Every state rule below targets .line, never a bare element selector. Each edge also
     carries a fat transparent .hit sibling, and '.edge.inferred path' ties
     '.edge path.hit' on specificity (0,3,1) -- so a bare-element rule wins on
     source order and paints the 12px hover target solid orange. That is what
     turned every flagged edge into a railroad tie. */
  .graph-svg .edge .line { fill: none; stroke: var(--vscode-descriptionForeground);
                           stroke-width: 1.5; opacity: 0.75;
                           stroke-linejoin: round; stroke-linecap: butt;
                           transition: opacity 120ms ease, stroke-width 120ms ease; }
  /* A fat invisible stroke so thin edges are still easy to hover. */
  .graph-svg .edge .hit { fill: none; stroke: transparent; stroke-width: 12;
                          pointer-events: stroke; }
  .graph-svg .edge.inferred .line { stroke-dasharray: 5 4; opacity: 0.7;
                                    stroke: var(--vscode-charts-orange, #d18616); }
  .graph-svg .edge.dimmed .line { opacity: 0.12; }
  .graph-svg .edge.dimmed .edge-label-g { opacity: 0; }
  .graph-svg .edge.active .line { stroke: var(--vscode-textLink-foreground); stroke-width: 2.4; opacity: 1; }
  .graph-svg marker path { fill: var(--vscode-descriptionForeground); stroke: none; }
  .graph-svg marker .inferred-head { fill: var(--vscode-charts-orange, #d18616); }

  /* Labels sit on a pill at the arc apex so they stay readable over whatever
     they cross. Hover-only in the sidebar; always on in the expanded tab,
     which has the room for them. */
  .graph-svg .edge-label-g { opacity: 0; transition: opacity 120ms ease; pointer-events: none; }
  .graph-svg.labels-on .edge-label-g { opacity: 0.9; }
  .graph-svg .edge.active .edge-label-g { opacity: 1; }
  .graph-svg .edge-label-bg { fill: var(--vscode-editor-background);
                              stroke: var(--vscode-panel-border); stroke-width: 0.8; }
  .graph-svg .edge.active .edge-label-bg { stroke: var(--vscode-textLink-foreground); }
  /* text-anchor is set per-label in the markup (centred on the track when labels
     are hover-only, hung off its right when they are always on). A CSS rule here
     would outrank that presentation attribute, so it must not set it. */
  .graph-svg .edge-label { font-size: 8.5px; fill: var(--vscode-foreground);
                           pointer-events: none; }
  .graph-svg.labels-on .edge-label { font-size: 10px; }

  /* Expanded tab: the timeline is the page, so let it take the height. */
  .graph-section.fullscreen .graph-viewport-outer { height: calc(100vh - 190px); min-height: 420px; }
  .graph-section.fullscreen .graph-hint { opacity: 0.75; }
  body:has(.graph-section.fullscreen) { max-width: none; }

  .graph-legend { display: flex; flex-wrap: wrap; gap: 5px 9px; margin-top: 8px; }
  .legend-chip { font-size: 0.68rem; opacity: 0.8; display: inline-flex; align-items: center; gap: 4px;
                border-left: 3px solid transparent; padding-left: 5px; }
  .legend-glyph { width: 13px; height: 13px; flex: none; fill: currentColor; }
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
    } else if (message.type === 'statusbar') {
      const note = document.getElementById('sb-note');
      const button = document.getElementById('backfill');
      if (note) {
        note.textContent = message.note || '';
        note.className = 'sb-note' + (message.isError ? ' error' : '');
        note.title = message.title || '';
      }
      if (button) {
        button.disabled = !!message.busy;
        button.textContent = message.label || 'Backfill';
      }
    }
  });

  // Open and highlight evidence card [index]. Shared by the [N] citations in the
  // synthesis and by a click on a Slack node in the timeline -- both are the same
  // gesture ("show me the thread this refers to") and must behave identically.
  function focusEvidence(index) {
    const card = document.getElementById('result-' + index);
    if (!card) { return false; }
    // The cards ship collapsed; a link that scrolled to a closed one would look
    // like it had gone nowhere.
    if (card.tagName === 'DETAILS') { card.open = true; }
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.remove('flash');
    void card.offsetWidth;
    card.classList.add('flash');
    return true;
  }

  // One delegated handler: the content below #app is replaced on every render, so
  // per-element listeners would have to be rebound (and would leak) each time.
  document.addEventListener('click', function (event) {
    const target = event.target;
    if (!(target instanceof Element)) { return; }

    const citation = target.closest('.citation');
    if (citation) {
      event.preventDefault();
      const id = citation.getAttribute('data-target') || '';
      focusEvidence(id.replace('result-', ''));
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
    if (button.id === 'backfill') { vscodeApi.postMessage({ type: 'backfill' }); }
    else if (button.id === 'send-to-agent') { vscodeApi.postMessage({ type: 'sendToAgent' }); }
    else if (button.id === 'refresh') { vscodeApi.postMessage({ type: 'refresh' }); }
    else if (button.id === 'retry') { vscodeApi.postMessage({ type: 'retry' }); }
    else if (button.id === 'reveal') { vscodeApi.postMessage({ type: 'reveal' }); }
    else if (button.id === 'clear-history') { vscodeApi.postMessage({ type: 'clearHistory' }); }
    else if (button.id === 'graph-expand') { vscodeApi.postMessage({ type: 'openTimeline' }); }
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

    // Fit the *width* only. Scaling a tall timeline to fit the viewport height would
    // shrink it to an unreadable sliver; the rail is meant to be panned down instead.
    function fit() {
      const rect = outer.getBoundingClientRect();
      if (rect.width === 0) { return; }
      const pad = 16;
      const fitScale = Math.min((rect.width - pad) / canvasWidth, 1);
      scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, fitScale || 1));
      tx = Math.max(0, (rect.width - canvasWidth * scale) / 2);
      ty = 8;
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

    // Keep the pan inside the canvas, so the rail can't be flung out of sight.
    function clampPan() {
      const rect = outer.getBoundingClientRect();
      const height = canvasHeight * scale;
      ty = Math.max(Math.min(8, rect.height - height - 8), Math.min(8, ty));
    }

    // A tall timeline wants the wheel to scroll it, the way every other long list in
    // the editor behaves. Zoom moves to ctrl/cmd+wheel.
    function onWheel(event) {
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) {
        zoomBy(event.deltaY < 0 ? 1.08 : 0.93, event.clientX, event.clientY);
        return;
      }
      ty -= event.deltaY;
      clampPan();
      apply();
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
      clampPan();
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
    const chipEls = Array.prototype.slice.call(svg.querySelectorAll('.chip-node'));

    // A chip is drawn inside its event's row, so it dims and lights with that event.
    function ownerOf(chip) {
      const row = chip.closest('.row');
      const owner = row ? row.querySelector('.node') : null;
      return owner ? owner.getAttribute('data-node-id') : null;
    }

    function setTrace(activeId) {
      if (!activeId) {
        nodeEls.forEach(function (n) { n.classList.remove('active', 'dimmed'); });
        edgeEls.forEach(function (e) { e.classList.remove('active', 'dimmed'); });
        chipEls.forEach(function (c) { c.classList.remove('dimmed'); });
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
      chipEls.forEach(function (c) {
        const owner = ownerOf(c);
        c.classList.toggle('dimmed', !(owner !== null && connected.has(owner)));
      });
    }

    const details = document.getElementById('node-details');
    const INTERNAL_KEYS = new Set(['permalink', 'citation', 'external_url', 'external_label']);
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
          // Internal bookkeeping, not detail for someone inspecting a node: the
          // link keys are what the Open button reads, and citation is how the
          // graph points back at an evidence card.
          return !INTERNAL_KEYS.has(key) &&
            data[key] !== undefined && data[key] !== null && data[key] !== '';
        })
        .map(function (key) {
          const value = Array.isArray(data[key]) ? data[key].join(', ') : data[key];
          return '<div class="nd-row"><span class="nd-key">' + escapeText(key) + '</span><span>' + escapeText(value) + '</span></div>';
        }).join('');
      const permalink = typeof data.permalink === 'string' ? data.permalink : '';
      const externalUrl = typeof data.external_url === 'string' ? data.external_url : '';
      const externalLabel = typeof data.external_label === 'string' ? data.external_label : 'Open externally';
      const openUrl = externalUrl || permalink;
      const openLabel = externalUrl ? externalLabel : 'Open in Slack';
      details.innerHTML =
        '<div class="nd-title"><strong>' + escapeText(node.type) + ': ' + escapeText(node.label) + '</strong></div>' +
        (rows || '<div class="nd-row muted">No further detail resolved for this node.</div>') +
        (openUrl ? '<div class="nd-open"><button id="nd-open-btn">' + escapeText(openLabel) + ' ↗</button></div>' : '');
      const openBtn = document.getElementById('nd-open-btn');
      if (openBtn) {
        openBtn.addEventListener('click', function () {
          vscodeApi.postMessage({ type: 'openLink', url: openUrl });
        });
      }
    }

    function parseNode(el) {
      try {
        return JSON.parse(el.getAttribute('data-node-json') || '{}');
      } catch (err) {
        return null;   // malformed payload -- leave the details panel untouched
      }
    }

    // Activating a node always fills the detail drawer; a node that carries a
    // citation *also* jumps to the evidence card it stands for, which is what makes
    // the timeline a navigable index of the list below rather than a separate view.
    function activate(el) {
      const node = parseNode(el);
      if (!node) { return; }
      renderDetails(node);
      const citation = el.getAttribute('data-citation');
      if (citation) { focusEvidence(citation); }
    }

    // The hover action takes a cited node to its evidence card. That card contains
    // the actual external link, so this preserves the page's reading flow and gives
    // the user the surrounding context before they leave VS Code. Nodes without a
    // citation still open their permalink directly when one is available.
    function openNode(el) {
      const node = parseNode(el);
      if (!node) { return; }
      const data = node.data || {};
      const citation = el.getAttribute('data-citation');
      if (citation && focusEvidence(citation)) {
        renderDetails(node);
        return;
      }
      // No evidence card to land on, so leave the editor: the thread in Slack, or
      // the PR / issue on the forge that resolved this node.
      const external = typeof data.permalink === 'string' && data.permalink
        ? data.permalink
        : (typeof data.external_url === 'string' ? data.external_url : '');
      if (external) {
        vscodeApi.postMessage({ type: 'openLink', url: external });
        renderDetails(node);
        return;
      }
      activate(el);
    }

    function wire(el) {
      // Satellite edges (AUTHORED_BY, TRACKED_BY) are folded into the card and so draw
      // no arc of their own -- tracing a chip by its own id would dim the whole graph.
      // Trace the event that owns it instead.
      const traceId = el.classList.contains('chip-node')
        ? ownerOf(el)
        : el.getAttribute('data-node-id');
      el.addEventListener('mouseenter', function () { setTrace(traceId); });
      el.addEventListener('mouseleave', function () { setTrace(null); });
      el.addEventListener('focus', function () { setTrace(traceId); });
      el.addEventListener('blur', function () { setTrace(null); });
      el.addEventListener('click', function (event) {
        event.stopPropagation();
        if (event.target instanceof Element && event.target.closest('.node-open')) {
          openNode(el);
          return;
        }
        activate(el);
      });
      el.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          activate(el);
        }
        // The action the hover button performs, without a pointer.
        if (event.key === 'o' || event.key === 'O') {
          event.preventDefault();
          openNode(el);
        }
      });
    }

    function clearSelection() {
      nodeEls.forEach(function (n) { n.classList.remove('selected'); });
      if (document.activeElement instanceof HTMLElement ||
          document.activeElement instanceof SVGElement) {
        document.activeElement.blur();   // otherwise the focus ring outlives the click
      }
      if (details) {
        details.classList.add('empty');
        details.textContent = 'Click a node above for its full detail.';
      }
    }

    // Node clicks stop propagating, so a click that reaches the canvas landed on
    // empty space: treat it as "nothing is selected" rather than leaving the last
    // card ringed with no way to undo it.
    outer.addEventListener('click', clearSelection);

    nodeEls.forEach(wire);
    chipEls.forEach(wire);

    destroyGraph = function () {
      outer.removeEventListener('click', clearSelection);
      outer.removeEventListener('wheel', onWheel);
      outer.removeEventListener('mousedown', onDown);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      window.removeEventListener('resize', fit);
    };

    // Re-fit whenever it is reopened: a collapsed <details> has no measurable box.
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
