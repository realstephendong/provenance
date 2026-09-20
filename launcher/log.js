'use strict';
// Terminal logger. Every line goes to the terminal and, once attach() is called, to a
// timestamped log file. Secrets are never passed in here; steps report "set"/"missing".

const fs = require('fs');
const path = require('path');

const useColor = process.stdout.isTTY && !process.env.NO_COLOR;
const paint = (code) => (s) => (useColor ? `\x1b[${code}m${s}\x1b[0m` : s);
const c = {
  green: paint('32'), yellow: paint('33'), red: paint('31'),
  dim: paint('2'), bold: paint('1'), cyan: paint('36'),
};

const STATUS = {
  running: { text: 'running', color: c.cyan },
  done: { text: 'done', color: c.green },
  skipped: { text: 'skipped', color: c.dim },
  warn: { text: 'warn', color: c.yellow },
  fail: { text: 'FAIL', color: c.red },
};

// eslint-disable-next-line no-control-regex
const stripAnsi = (s) => s.replace(/\x1b\[[0-9;]*m/g, '');

class Logger {
  constructor() {
    this.file = null;
    this.titleWidth = 0;
  }

  attach(filePath) {
    try {
      fs.mkdirSync(path.dirname(filePath), { recursive: true });
      // Keep the log from growing forever: start each run from the tail of the last one.
      if (fs.existsSync(filePath) && fs.statSync(filePath).size > 512 * 1024) {
        const tail = fs.readFileSync(filePath, 'utf8').split('\n').slice(-500).join('\n');
        fs.writeFileSync(filePath, tail);
      }
      this.file = filePath;
    } catch {
      this.file = null; // logging to a file is best-effort
    }
  }

  _emit(line) {
    console.log(line);
    if (this.file) {
      try {
        fs.appendFileSync(this.file, `${new Date().toISOString()} ${stripAnsi(line)}\n`);
      } catch { /* best-effort */ }
    }
  }

  banner(text) { this._emit(c.bold(text)); }
  info(text) { this._emit(`  ${text}`); }
  blank() { this._emit(''); }

  /** One status line for a step: "  [3/7] Python environment   done   note (12s)". */
  step(label, title, status, note, elapsedMs) {
    const s = STATUS[status];
    const time = elapsedMs !== undefined && elapsedMs >= 2000
      ? c.dim(` (${(elapsedMs / 1000).toFixed(0)}s)`) : '';
    const pad = ' '.repeat(Math.max(1, this.titleWidth - title.length + 2));
    const tail = note ? `  ${note}` : '';
    this._emit(`  ${c.dim(label)} ${title}${pad}${s.color(s.text.padEnd(7))}${tail}${time}`);
  }

  /** Streamed child-process output, indented under the step it belongs to. */
  detail(line) {
    if (line.trim() === '') return;
    this._emit(`        ${c.dim('|')} ${line}`);
  }

  error(text) { this._emit(c.red(`  ${text}`)); }
  warn(text) { this._emit(c.yellow(`  ${text}`)); }
  hint(text) { this._emit(`  ${c.cyan('hint:')} ${text}`); }

  summary(rows) {
    const width = Math.max(...rows.map(([k]) => k.length));
    this.blank();
    this._emit(c.bold('  Ready.'));
    for (const [k, v] of rows) this._emit(`    ${k.padEnd(width)}  ${v}`);
  }
}

module.exports = { Logger, c };
