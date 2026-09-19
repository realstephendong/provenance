import * as vscode from 'vscode';
import { postCount } from './api';
import { Selection } from './types';

// Off by default (`provenance.codeLens`). One HTTP request per top-level symbol per
// file open is a real cost, not a hypothetical one -- so the lens uses the cheap
// /context/count path, caches per document, and invalidates only on edit.

const DECLARATION = new RegExp(
  '^(?:export\\s+)?(?:async\\s+)?' +
  '(?:def|class|function|func|fn|const|let|var|interface|type|struct|impl)\\s+' +
  '([A-Za-z_][A-Za-z0-9_]*)',
);

const MAX_BODY_LINES = 120;

interface Declaration {
  name: string;
  startLine: number;   // 0-indexed
  endLine: number;     // 0-indexed, inclusive
}

/** Body end by indentation: the last line indented deeper than the declaration,
 *  capped so a lens stays cheap on a very long function. */
function findDeclarations(document: vscode.TextDocument): Declaration[] {
  const declarations: Declaration[] = [];

  for (let line = 0; line < document.lineCount; line += 1) {
    const text = document.lineAt(line).text;
    if (/^\s/.test(text)) { continue; }          // top-level only
    const match = DECLARATION.exec(text);
    if (!match) { continue; }

    const limit = Math.min(document.lineCount - 1, line + MAX_BODY_LINES);
    let end = line;
    for (let next = line + 1; next <= limit; next += 1) {
      const candidate = document.lineAt(next).text;
      if (candidate.trim().length === 0) { continue; }
      if (!/^\s/.test(candidate)) { break; }
      end = next;
    }
    declarations.push({ name: match[1], startLine: line, endLine: end });
  }
  return declarations;
}

class CountingCodeLens extends vscode.CodeLens {
  constructor(
    range: vscode.Range,
    readonly selection: Selection,
    readonly cacheKey: string,
    /** Must match the key the invalidation listeners delete: the document URI. */
    readonly documentKey: string,
  ) {
    super(range);
  }
}

export class ProvenanceCodeLensProvider implements vscode.CodeLensProvider {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this.changed.event;

  /** documentUri -> (declarationKey -> rendered title) */
  private cache = new Map<string, Map<string, string>>();

  constructor(private readonly disposables: vscode.Disposable[]) {
    this.disposables.push(
      vscode.workspace.onDidChangeTextDocument((event) => {
        this.cache.delete(event.document.uri.toString());
        this.changed.fire();
      }),
      vscode.workspace.onDidCloseTextDocument((document) => {
        this.cache.delete(document.uri.toString());
      }),
      vscode.workspace.onDidChangeConfiguration((event) => {
        if (event.affectsConfiguration('provenance')) {
          this.cache.clear();
          this.changed.fire();
        }
      }),
    );
  }

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    if (!vscode.workspace.getConfiguration('provenance').get<boolean>('codeLens', true)) {
      return [];
    }
    const folder = vscode.workspace.getWorkspaceFolder(document.uri);
    if (!folder) { return []; }

    return findDeclarations(document).map((declaration) => {
      const range = new vscode.Range(declaration.startLine, 0, declaration.startLine, 0);
      const selection: Selection = {
        code: document.getText(new vscode.Range(
          declaration.startLine, 0, declaration.endLine, Number.MAX_SAFE_INTEGER,
        )),
        file_path: vscode.workspace.asRelativePath(document.uri, false),
        repo_root: folder.uri.fsPath,
        line_start: declaration.startLine + 1,
        line_end: declaration.endLine + 1,
        language: document.languageId,
      };
      return new CountingCodeLens(
        range, selection, `${declaration.name}:${declaration.startLine}`,
        document.uri.toString(),
      );
    });
  }

  async resolveCodeLens(lens: vscode.CodeLens): Promise<vscode.CodeLens> {
    if (!(lens instanceof CountingCodeLens)) { return lens; }

    const documentKey = lens.documentKey;
    const perDocument = this.cache.get(documentKey) ?? new Map<string, string>();
    const cached = perDocument.get(lens.cacheKey);
    if (cached !== undefined) {
      lens.command = this.commandFor(cached, lens);
      return lens;
    }

    const base = vscode.workspace
      .getConfiguration('provenance')
      .get<string>('serviceUrl', 'http://127.0.0.1:8000');

    let title = '';
    try {
      const count = await postCount(base, lens.selection);
      if (count.count > 0) {
        title = count.has_exact
          ? `${count.count} discussions · exact match`
          : `${count.count} discussions`;
      }
    } catch {
      title = '';   // a lens must never surface an error in the gutter
    }

    perDocument.set(lens.cacheKey, title);
    this.cache.set(documentKey, perDocument);
    lens.command = this.commandFor(title, lens);
    return lens;
  }

  private commandFor(title: string, lens: CountingCodeLens): vscode.Command {
    return {
      title: title || '',
      command: title ? 'provenance.explainRange' : '',
      arguments: [lens.selection],
    };
  }

  dispose(): void {
    this.changed.dispose();
  }
}
