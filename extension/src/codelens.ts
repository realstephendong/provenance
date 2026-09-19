import * as vscode from "vscode";
import { Selection } from "./types";

/**
 * "3 discussions" above each top-level declaration.
 *
 * Visually striking, and cheap once /context works -- but it issues one
 * request per symbol per file open, so it ships behind a setting that is off
 * by default. Turn it on for the demo, not for daily use.
 */

const DECL = [
  /^\s*(?:async\s+)?def\s+(\w+)/, // python
  /^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)/, // js/ts
  /^\s*func\s+(?:\([^)]*\)\s*)?(\w+)/, // go
  /^\s*(?:public|private|protected)?\s*class\s+(\w+)/, // classes
];

interface CountResponse {
  count: number;
  has_exact: boolean;
}

export class HindsightCodeLensProvider implements vscode.CodeLensProvider {
  private cache = new Map<string, CountResponse>();
  private emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this.emitter.event;

  constructor() {
    // A file edit invalidates its counts; re-query lazily on next resolve.
    vscode.workspace.onDidChangeTextDocument((e) => {
      const prefix = e.document.uri.toString();
      for (const key of [...this.cache.keys()]) {
        if (key.startsWith(prefix)) {
          this.cache.delete(key);
        }
      }
    });
  }

  provideCodeLenses(doc: vscode.TextDocument): vscode.CodeLens[] {
    if (!enabled()) {
      return [];
    }
    const lenses: vscode.CodeLens[] = [];
    for (let line = 0; line < doc.lineCount; line++) {
      const text = doc.lineAt(line).text;
      if (DECL.some((re) => re.test(text))) {
        lenses.push(new vscode.CodeLens(new vscode.Range(line, 0, line, 0)));
      }
    }
    return lenses;
  }

  async resolveCodeLens(
    lens: vscode.CodeLens,
    token: vscode.CancellationToken
  ): Promise<vscode.CodeLens> {
    const editor = vscode.window.activeTextEditor;
    const doc = editor?.document;
    if (!doc) {
      lens.command = { title: "", command: "" };
      return lens;
    }

    const start = lens.range.start.line;
    const end = bodyEnd(doc, start);
    const selection = toSelection(doc, start, end);
    const key = `${doc.uri.toString()}#${start}-${end}`;

    let counts = this.cache.get(key);
    if (!counts) {
      counts = await this.fetchCount(selection, token);
      if (counts) {
        this.cache.set(key, counts);
      }
    }

    if (!counts || counts.count === 0) {
      lens.command = { title: "", command: "" };
      return lens;
    }

    const noun = counts.count === 1 ? "discussion" : "discussions";
    const badge = counts.has_exact ? " · exact match" : "";
    lens.command = {
      title: `${counts.count} ${noun}${badge}`,
      command: "hindsight.explainRange",
      arguments: [start, end],
    };
    return lens;
  }

  private async fetchCount(
    selection: Selection,
    token: vscode.CancellationToken
  ): Promise<CountResponse | undefined> {
    const base = vscode.workspace
      .getConfiguration("hindsight")
      .get<string>("serviceUrl", "http://127.0.0.1:8000");
    const controller = new AbortController();
    token.onCancellationRequested(() => controller.abort());
    try {
      const resp = await fetch(`${base}/context/count`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(selection),
        signal: controller.signal,
      });
      if (!resp.ok) {
        return undefined;
      }
      return (await resp.json()) as CountResponse;
    } catch {
      return undefined; // CodeLens must never surface an error to the editor
    }
  }
}

export function enabled(): boolean {
  return vscode.workspace
    .getConfiguration("hindsight")
    .get<boolean>("codeLens", false);
}

/** End of the declaration's body, by indentation, capped so a lens stays cheap. */
function bodyEnd(doc: vscode.TextDocument, start: number): number {
  const baseIndent = indentOf(doc.lineAt(start).text);
  let end = start;
  for (let line = start + 1; line < doc.lineCount && line < start + 120; line++) {
    const text = doc.lineAt(line).text;
    if (!text.trim()) {
      continue;
    }
    if (indentOf(text) <= baseIndent) {
      break;
    }
    end = line;
  }
  return end;
}

function indentOf(text: string): number {
  return text.length - text.trimStart().length;
}

export function toSelection(
  doc: vscode.TextDocument,
  startLine: number,
  endLine: number
): Selection {
  const range = new vscode.Range(startLine, 0, endLine, Number.MAX_SAFE_INTEGER);
  const folder = vscode.workspace.getWorkspaceFolder(doc.uri);
  return {
    code: doc.getText(range),
    file_path: vscode.workspace.asRelativePath(doc.uri, false),
    repo_root: folder ? folder.uri.fsPath : "",
    line_start: startLine + 1,
    line_end: endLine + 1,
    language: doc.languageId,
  };
}
