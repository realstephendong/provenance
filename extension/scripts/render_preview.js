#!/usr/bin/env node
/**
 * Render the real panel HTML to a file, without launching an extension host.
 *
 *   node scripts/render_preview.js [file] [start] [end]
 *   open preview.html
 *
 * Trap #5: the launch-a-second-window cycle is slow and demoralising at hour
 * 20. This gives the panel a sub-second edit loop for everything except the
 * VS Code message passing.
 */

const fs = require("fs");
const path = require("path");
const Module = require("module");

// Minimal `vscode` stub: the renderers only touch ViewColumn and the theme
// CSS variables, which resolve in a plain browser to nothing (hence the
// fallbacks below).
const stub = {
  ViewColumn: { Beside: 2 },
  window: { createWebviewPanel: () => { throw new Error("not used"); } },
  workspace: { getConfiguration: () => ({ get: (_k, d) => d }) },
  env: {},
  Uri: {},
};
const origResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...args) {
  if (request === "vscode") return "vscode";
  return origResolve.call(this, request, ...args);
};
require.cache["vscode"] = { id: "vscode", filename: "vscode", loaded: true, exports: stub };

const panel = require("../out/panel.js");

const ROOT = path.resolve(__dirname, "../..");
const REPO = path.join(ROOT, "seed", "repo");
const SERVICE = process.env.HINDSIGHT_SERVICE_URL || "http://127.0.0.1:8000";

const file = process.argv[2] || "webhooks/delivery.py";
const start = Number(process.argv[3] || 23);
const end = Number(process.argv[4] || 68);

(async () => {
  const lines = fs.readFileSync(path.join(REPO, file), "utf8").split("\n");
  const selection = {
    code: lines.slice(start - 1, end).join("\n"),
    file_path: file,
    repo_root: REPO,
    line_start: start,
    line_end: end,
    language: "python",
  };

  const resp = await fetch(`${SERVICE}/context`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(selection),
  });
  if (!resp.ok) {
    console.error(`service returned ${resp.status}. Is it running? (make serve)`);
    process.exit(1);
  }
  const data = await resp.json();

  // Approximate the VS Code theme so the preview is readable in a browser.
  const theme = `<style>
    :root {
      --vscode-font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --vscode-font-size: 13px;
      --vscode-foreground: #cccccc;
      --vscode-editor-background: #1f1f1f;
      --vscode-descriptionForeground: #9d9d9d;
      --vscode-panel-border: #3c3c3c;
      --vscode-textLink-foreground: #4daafc;
      --vscode-textBlockQuote-background: #2a2a2a;
      --vscode-focusBorder: #0078d4;
      --vscode-button-background: #0078d4;
      --vscode-button-foreground: #ffffff;
      --vscode-button-hoverBackground: #026ec1;
    }
    body { background: #1f1f1f; }
  </style>`;

  let html = panel.resultHtml(data, selection);
  html = html
    .replace("</head>", theme + "</head>")
    .replace("acquireVsCodeApi()", "({ postMessage: (m) => console.log(m) })");

  const out = path.join(__dirname, "..", "preview.html");
  fs.writeFileSync(out, html);
  console.log(`wrote ${out}`);
  console.log(`  results: ${data.results.length}`);
  console.log(`  exact:   ${data.results.filter((r) => r.match_type === "exact").length}`);
  console.log(`  timing:  ${JSON.stringify(data.timing_ms)}`);
})();
