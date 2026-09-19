import * as vscode from "vscode";
import { ContextResponse, Result, Selection } from "./types";

// Four seconds of blank panel reads as broken; four seconds of "Reading git
// history..." reads as working. These are optimistic -- the service is one
// request -- but they track the real pipeline stages closely enough.
const STAGES = [
  "Reading git history…",
  "Resolving the pull request…",
  "Describing the code…",
  "Searching Slack…",
  "Ranking discussions…",
  "Writing the answer…",
];

export class HindsightPanel {
  private static current: HindsightPanel | undefined;
  private readonly panel: vscode.WebviewPanel;
  private disposables: vscode.Disposable[] = [];
  private stageTimer: NodeJS.Timeout | undefined;
  private last: { data: ContextResponse; selection: Selection } | undefined;

  static show(selection: Selection): HindsightPanel {
    if (HindsightPanel.current) {
      HindsightPanel.current.panel.reveal(vscode.ViewColumn.Beside, true);
    } else {
      HindsightPanel.current = new HindsightPanel();
    }
    HindsightPanel.current.showLoading(selection);
    return HindsightPanel.current;
  }

  private constructor() {
    // Reused across invocations: a new tab per lookup gets old fast.
    this.panel = vscode.window.createWebviewPanel(
      "hindsight",
      "Hindsight",
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
      { enableScripts: true, retainContextWhenHidden: true }
    );

    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
    this.panel.webview.onDidReceiveMessage(
      (msg) => this.onMessage(msg),
      null,
      this.disposables
    );
  }

  private async onMessage(msg: { command: string; url?: string }) {
    if (msg.command === "openSlack" && msg.url) {
      await vscode.env.openExternal(vscode.Uri.parse(msg.url));
      return;
    }
    if (msg.command === "sendToAgent" && this.last) {
      await sendToAgent(this.last.data, this.last.selection);
    }
  }

  private showLoading(selection: Selection) {
    this.last = undefined;
    const label = `${selection.file_path}:${selection.line_start}-${selection.line_end}`;
    let i = 0;
    const paint = () => {
      this.panel.webview.html = loadingHtml(label, STAGES[Math.min(i, STAGES.length - 1)]);
      i += 1;
    };
    paint();
    clearInterval(this.stageTimer);
    this.stageTimer = setInterval(paint, 900);
  }

  showResult(data: ContextResponse, selection: Selection) {
    clearInterval(this.stageTimer);
    this.stageTimer = undefined;
    this.last = { data, selection };
    this.panel.webview.html = resultHtml(data, selection);
  }

  showError(message: string) {
    clearInterval(this.stageTimer);
    this.stageTimer = undefined;
    this.panel.webview.html = errorHtml(message);
  }

  dispose() {
    clearInterval(this.stageTimer);
    HindsightPanel.current = undefined;
    while (this.disposables.length) {
      this.disposables.pop()?.dispose();
    }
    this.panel.dispose();
  }
}

// --- Send to agent --------------------------------------------------------

export function agentMarkdown(data: ContextResponse, selection: Selection): string {
  const lines: string[] = [
    `## Team context for ${selection.file_path}:${selection.line_start}-${selection.line_end}`,
    "",
  ];

  const b = data.blame;
  if (b?.dominant_sha) {
    const pr = b.pr_number ? `, PR #${b.pr_number}` : "";
    lines.push(
      `Last touched by ${b.authors.join(", ") || "unknown"} on ${b.commit_date} (${b.dominant_sha}${pr}).`,
      ""
    );
  }

  if (data.synthesis) {
    lines.push(data.synthesis, "");
  }

  for (const r of data.results) {
    lines.push(`### ${r.channel_name} - ${r.date}`, "", r.raw_text || r.summary, "");
  }
  return lines.join("\n");
}

async function sendToAgent(data: ContextResponse, selection: Selection) {
  const markdown = agentMarkdown(data, selection);

  // The clipboard is the reliable path; the file is what makes the agent
  // story concrete. Do both, and never let the file failure eat the copy.
  await vscode.env.clipboard.writeText(markdown);

  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    vscode.window.showInformationMessage("Hindsight: context copied to clipboard.");
    return;
  }

  const rel = vscode.workspace
    .getConfiguration("hindsight")
    .get<string>("contextFile", ".hindsight/context.md");
  const target = vscode.Uri.joinPath(folder.uri, rel);

  try {
    await vscode.workspace.fs.createDirectory(
      vscode.Uri.joinPath(target, "..")
    );
    let existing = "";
    try {
      existing = new TextDecoder().decode(
        await vscode.workspace.fs.readFile(target)
      );
    } catch {
      // first write
    }
    await vscode.workspace.fs.writeFile(
      target,
      new TextEncoder().encode(existing + markdown + "\n\n")
    );
    vscode.window.showInformationMessage(
      `Hindsight: context copied to clipboard and appended to ${rel}.`
    );
  } catch (err) {
    vscode.window.showWarningMessage(
      `Hindsight: copied to clipboard, but could not write ${rel} (${err}).`
    );
  }
}

// --- HTML -----------------------------------------------------------------

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function shell(body: string, script = ""): string {
  return `<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  :root { color-scheme: light dark; }
  body {
    font-family: var(--vscode-font-family);
    font-size: var(--vscode-font-size);
    color: var(--vscode-foreground);
    padding: 16px 18px 40px;
    line-height: 1.5;
  }
  .blame {
    font-size: 0.85em;
    color: var(--vscode-descriptionForeground);
    border-bottom: 1px solid var(--vscode-panel-border);
    padding-bottom: 10px;
    margin-bottom: 16px;
  }
  .blame strong { color: var(--vscode-foreground); font-weight: 600; }
  .synthesis {
    font-size: 1.05em;
    margin: 0 0 22px;
    padding: 12px 14px;
    background: var(--vscode-textBlockQuote-background);
    border-left: 3px solid var(--vscode-textLink-foreground);
    border-radius: 3px;
  }
  .cite {
    color: var(--vscode-textLink-foreground);
    text-decoration: none;
    font-weight: 600;
    cursor: pointer;
  }
  .card {
    border: 1px solid var(--vscode-panel-border);
    border-radius: 5px;
    padding: 12px 14px;
    margin-bottom: 12px;
  }
  .card.exact { border-color: var(--vscode-textLink-foreground); }
  .card:target { outline: 2px solid var(--vscode-focusBorder); }
  .head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
  .num { color: var(--vscode-descriptionForeground); font-weight: 600; }
  .channel { font-weight: 600; }
  .date, .who { color: var(--vscode-descriptionForeground); font-size: 0.85em; }
  .badge {
    background: var(--vscode-textLink-foreground);
    color: var(--vscode-editor-background);
    font-size: 0.72em;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 2px 7px;
    border-radius: 3px;
  }
  .why { font-style: italic; margin: 8px 0 6px; }
  .summary { color: var(--vscode-descriptionForeground); font-size: 0.93em; }
  a.slack {
    display: inline-block;
    margin-top: 9px;
    font-size: 0.85em;
    color: var(--vscode-textLink-foreground);
    text-decoration: none;
    cursor: pointer;
  }
  a.slack:hover { text-decoration: underline; }
  button {
    background: var(--vscode-button-background);
    color: var(--vscode-button-foreground);
    border: none; border-radius: 3px;
    padding: 8px 15px; cursor: pointer; font-size: 0.95em;
  }
  button:hover { background: var(--vscode-button-hoverBackground); }
  .empty { color: var(--vscode-descriptionForeground); padding: 28px 0; text-align: center; }
  .spinner {
    width: 15px; height: 15px; display: inline-block; vertical-align: -2px;
    border: 2px solid var(--vscode-descriptionForeground);
    border-top-color: transparent; border-radius: 50%;
    animation: spin 0.7s linear infinite; margin-right: 9px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .timing { margin-top: 22px; font-size: 0.75em; color: var(--vscode-descriptionForeground); }
</style></head>
<body>${body}<script>${script}</script></body></html>`;
}

export function loadingHtml(label: string, stage: string): string {
  return shell(`
    <div class="blame">${escapeHtml(label)}</div>
    <p><span class="spinner"></span>${escapeHtml(stage)}</p>
  `);
}

function errorHtml(message: string): string {
  return shell(`<div class="empty">${escapeHtml(message)}</div>`);
}

function blameLine(data: ContextResponse): string {
  const b = data.blame;
  if (!b || (!b.dominant_sha && !b.uncommitted)) {
    return "No git history for this selection.";
  }
  if (!b.dominant_sha) {
    return "These lines are uncommitted, so there is no commit to anchor on.";
  }
  const who = b.authors.join(", ") || "unknown";
  const pr = b.pr_number ? `, PR #${b.pr_number}` : "";
  return `Last touched by <strong>${escapeHtml(who)}</strong>, ${escapeHtml(
    b.commit_date || ""
  )}${escapeHtml(pr)} <span class="date">(${escapeHtml(b.dominant_sha)})</span>`;
}

function cardHtml(r: Result, i: number): string {
  const badge =
    r.match_type === "exact" ? `<span class="badge">Exact match</span>` : "";
  return `
  <div class="card ${r.match_type}" id="card-${i}">
    <div class="head">
      <span class="num">[${i}]</span>
      <span class="channel">#${escapeHtml(r.channel_name)}</span>
      <span class="date">${escapeHtml(r.date)}</span>
      ${badge}
    </div>
    <div class="who">${escapeHtml(r.participants.slice(0, 6).join(", "))}</div>
    ${r.why ? `<div class="why">${escapeHtml(r.why)}</div>` : ""}
    <div class="summary">${escapeHtml(r.summary)}</div>
    <a class="slack" data-url="${escapeHtml(r.permalink)}">Open in Slack ↗</a>
  </div>`;
}

export function resultHtml(data: ContextResponse, selection: Selection): string {
  const header = `<div class="blame">${blameLine(data)}</div>`;

  if (!data.results.length) {
    const msg = data.message || "No relevant discussions found for this code.";
    return shell(
      header +
        `<div class="empty">${escapeHtml(msg)}</div>` +
        timingHtml(data.timing_ms)
    );
  }

  // [1] markers become anchors that scroll to the matching card.
  const synthesis = data.synthesis
    ? `<div class="synthesis">${escapeHtml(data.synthesis).replace(
        /\[(\d+)\]/g,
        '<a class="cite" href="#card-$1">[$1]</a>'
      )}</div>`
    : "";

  const cards = data.results.map((r, idx) => cardHtml(r, idx + 1)).join("");
  const button = `<button id="send">Send to agent</button>`;

  const script = `
    const vscode = acquireVsCodeApi();
    document.getElementById('send').addEventListener('click', () => {
      vscode.postMessage({ command: 'sendToAgent' });
    });
    for (const a of document.querySelectorAll('a.slack')) {
      a.addEventListener('click', () => {
        vscode.postMessage({ command: 'openSlack', url: a.dataset.url });
      });
    }
    for (const c of document.querySelectorAll('a.cite')) {
      c.addEventListener('click', (e) => {
        e.preventDefault();
        const el = document.querySelector(c.getAttribute('href'));
        if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
      });
    }
  `;

  return shell(
    header + synthesis + cards + button + timingHtml(data.timing_ms),
    script
  );
}

function timingHtml(timing: Record<string, number>): string {
  if (!timing || !timing.total) {
    return "";
  }
  const parts = Object.entries(timing)
    .filter(([k]) => k !== "total")
    .map(([k, v]) => `${k} ${v}ms`)
    .join(" · ");
  return `<div class="timing">${escapeHtml(
    `${timing.total}ms total`
  )}${parts ? ` — ${escapeHtml(parts)}` : ""}</div>`;
}
