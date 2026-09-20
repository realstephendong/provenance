'use strict';
// Shared helpers. Node built-ins only.

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { spawn } = require('child_process');

const IS_WIN = process.platform === 'win32';
const IS_MAC = process.platform === 'darwin';

/** A failure the runner can print cleanly: a message, an optional hint, an optional log tail. */
class StepError extends Error {
  constructor(message, { hint, logTail } = {}) {
    super(message);
    this.name = 'StepError';
    this.hint = hint;
    this.logTail = logTail;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --- executables ------------------------------------------------------------------

function isFile(p) {
  try { return fs.statSync(p).isFile(); } catch { return false; }
}

/** Resolve a command on PATH (honouring PATHEXT on Windows). Returns a full path or null. */
function which(name) {
  if (path.isAbsolute(name) || name.includes('/') || name.includes('\\')) {
    return isFile(name) ? name : null;
  }
  const dirs = (process.env.PATH || '').split(path.delimiter).filter(Boolean);
  const exts = IS_WIN
    ? ['', ...(process.env.PATHEXT || '.COM;.EXE;.BAT;.CMD').split(';')]
    : [''];
  for (const dir of dirs) {
    for (const ext of exts) {
      // On Windows a bare "code" (no extension) is a POSIX shell script; only accept
      // extensionless matches if the caller already gave an extension.
      if (IS_WIN && ext === '' && !path.extname(name)) continue;
      const candidate = path.join(dir, name + ext);
      if (isFile(candidate)) return candidate;
    }
  }
  return null;
}

// cmd.exe treats these specially even inside double quotes (or can't be quoted at all).
const CMD_UNSAFE = /[%"^&|<>\r\n]/;

function quoteForCmd(arg) {
  if (CMD_UNSAFE.test(arg)) {
    throw new StepError(
      `Refusing to pass "${arg}" to a Windows .cmd command: it contains characters cmd.exe would misinterpret (% " ^ & | < >).`,
      { hint: 'Rename the folder or open it in VS Code manually.' },
    );
  }
  return /[\s()]/.test(arg) || arg === '' ? `"${arg}"` : arg;
}

/** Kill a process and its children. */
function killTree(pid, { force = false, group = false } = {}) {
  if (IS_WIN) {
    spawn('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    return;
  }
  const sig = force ? 'SIGKILL' : 'SIGTERM';
  try { process.kill(group ? -pid : pid, sig); } catch { /* already gone */ }
}

const isPidAlive = (pid) => {
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
};

/**
 * Run a command, streaming each output line to opts.onLine.
 * Resolves { code, lines, output, error } and never rejects on a non-zero exit.
 * On Windows, .cmd/.bat launchers (npm, code) need a shell (Node >= 20.12 refuses to
 * spawn them otherwise), so their arguments are quoted and checked first.
 */
function run(cmd, args = [], opts = {}) {
  const { cwd, env, onLine, timeoutMs } = opts;
  const resolved = which(cmd) || cmd;
  const useShell = IS_WIN && /\.(cmd|bat)$/i.test(resolved);
  const shellLine = useShell ? [resolved, ...args].map(quoteForCmd).join(' ') : null;

  return new Promise((resolve) => {
    const lines = [];
    let child;
    try {
      // stdin is closed so a tool that prompts (vsce, npm) fails instead of hanging.
      const stdio = ['ignore', 'pipe', 'pipe'];
      child = useShell
        ? spawn(shellLine, { cwd, env, shell: true, windowsHide: true, stdio })
        : spawn(resolved, args, { cwd, env, windowsHide: true, stdio });
    } catch (error) {
      resolve({ code: -1, lines, output: '', error });
      return;
    }

    let spawnError = null;
    let timedOut = false;
    const timer = timeoutMs
      ? setTimeout(() => { timedOut = true; killTree(child.pid); }, timeoutMs) : null;

    const pending = { out: '', err: '' };
    const feed = (key) => (chunk) => {
      pending[key] += chunk.toString();
      const parts = pending[key].split(/\r\n|\n|\r/);
      pending[key] = parts.pop();
      for (const line of parts) {
        lines.push(line);
        if (lines.length > 400) lines.shift();
        if (onLine) onLine(line);
      }
    };
    child.stdout.on('data', feed('out'));
    child.stderr.on('data', feed('err'));
    child.on('error', (e) => { spawnError = e; });
    child.on('close', (code) => {
      if (timer) clearTimeout(timer);
      for (const key of ['out', 'err']) {
        if (pending[key]) { lines.push(pending[key]); if (onLine) onLine(pending[key]); }
      }
      resolve({
        code: spawnError ? -1 : (timedOut ? -2 : code),
        lines,
        output: lines.join('\n'),
        error: spawnError,
        timedOut,
      });
    });
  });
}

/** run() that throws a StepError (with the output tail) on any failure. */
async function runOrThrow(cmd, args, opts, what, hint) {
  const r = await run(cmd, args, opts);
  if (r.code !== 0) {
    const why = r.error ? r.error.message : r.timedOut ? 'timed out' : `exit code ${r.code}`;
    throw new StepError(`${what} failed (${why})`, { hint, logTail: r.lines.slice(-15) });
  }
  return r;
}

// --- waiting / HTTP -----------------------------------------------------------------

/**
 * Poll fn() until it returns something truthy. Returns that value, or null on timeout.
 * abortIf() may return a message to fail immediately (e.g. the child process died).
 * onTick(elapsedMs) is called after each unsuccessful poll, for progress output.
 */
async function waitFor(fn, { timeoutMs, intervalMs = 1000, abortIf, onTick } = {}) {
  const start = Date.now();
  for (;;) {
    const value = await fn();
    if (value) return value;
    if (abortIf) {
      const message = await abortIf();
      if (message) throw new StepError(message);
    }
    const elapsed = Date.now() - start;
    if (elapsed >= timeoutMs) return null;
    if (onTick) onTick(elapsed);
    await sleep(intervalMs);
  }
}

/** An onTick that prints "<what> ... Ns" at most every `everyMs`. */
function progress(log, what, everyMs = 15000) {
  let next = everyMs;
  return (elapsed) => {
    if (elapsed >= next) {
      log.detail(`${what} (${Math.round(elapsed / 1000)}s)`);
      next += everyMs;
    }
  };
}

/** GET a URL. Returns { status, json, text } or null if nothing answered. */
async function httpGetJson(url, timeoutMs = 3000) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch { /* not JSON */ }
    return { status: res.status, json, text };
  } catch {
    return null;
  }
}

// --- files ----------------------------------------------------------------------------

function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; }
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(value, null, 2));
}

function walk(dir, skip = new Set(['node_modules', 'out', '.git'])) {
  const found = [];
  let entries = [];
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return found; }
  for (const entry of entries) {
    if (skip.has(entry.name)) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) found.push(...walk(full, skip));
    else found.push(full);
  }
  return found;
}

/** One hash over several files' relative names and contents. Missing files are ignored. */
function hashFiles(files, base) {
  const h = crypto.createHash('sha256');
  for (const file of [...files].sort()) {
    if (!isFile(file)) continue;
    h.update(path.relative(base, file).replace(/\\/g, '/'));
    h.update('\0');
    h.update(fs.readFileSync(file));
    h.update('\0');
  }
  return h.digest('hex');
}

function tailFile(file, n = 20) {
  try {
    const size = fs.statSync(file).size;
    const fd = fs.openSync(file, 'r');
    const len = Math.min(size, 64 * 1024);
    const buf = Buffer.alloc(len);
    fs.readSync(fd, buf, 0, len, size - len);
    fs.closeSync(fd);
    return buf.toString('utf8').split(/\r?\n/).filter(Boolean).slice(-n);
  } catch {
    return [];
  }
}

/** Print the tail of a file, then keep printing new lines until the process is interrupted. */
async function followFile(file, { initialLines = 20, onLine }) {
  for (const line of tailFile(file, initialLines)) onLine(line);
  let pos = 0;
  try { pos = fs.statSync(file).size; } catch { /* appears later */ }
  let partial = '';
  for (;;) {
    await sleep(500);
    let size;
    try { size = fs.statSync(file).size; } catch { continue; }
    if (size < pos) pos = 0; // truncated: service restarted
    if (size === pos) continue;
    const fd = fs.openSync(file, 'r');
    const buf = Buffer.alloc(size - pos);
    fs.readSync(fd, buf, 0, buf.length, pos);
    fs.closeSync(fd);
    pos = size;
    const parts = (partial + buf.toString('utf8')).split(/\r?\n/);
    partial = parts.pop();
    for (const line of parts) onLine(line);
  }
}

// --- dotenv -----------------------------------------------------------------------------

/** Small .env parser: comments, `export`, single/double quotes, trailing " # comment". */
function parseDotenv(text) {
  const out = {};
  for (const raw of text.replace(/^﻿/, '').split(/\r?\n/)) {
    let line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    if (line.startsWith('export ')) line = line.slice(7).trim();
    const eq = line.indexOf('=');
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_.-]*$/.test(key)) continue;
    let val = line.slice(eq + 1).trim();
    if (val.startsWith('"') || val.startsWith("'")) {
      const quote = val[0];
      const end = val.indexOf(quote, 1);
      val = end === -1 ? val.slice(1) : val.slice(1, end);
      if (quote === '"') val = val.replace(/\\n/g, '\n').replace(/\\"/g, '"');
    } else {
      const hash = val.search(/\s#/);
      if (hash !== -1) val = val.slice(0, hash).trim();
    }
    out[key] = val;
  }
  return out;
}

// --- tool discovery -----------------------------------------------------------------------

/** Find a Python >= 3.12. Returns { cmd, args, version } or null. */
async function findPython(env) {
  const candidates = [];
  if (env.PROVENANCE_PYTHON) candidates.push([env.PROVENANCE_PYTHON, []]);
  if (IS_WIN) {
    candidates.push(['py', ['-3.13']], ['py', ['-3.12']]);
  }
  candidates.push(['python3.13', []], ['python3.12', []], ['python3', []], ['python', []]);
  if (IS_WIN) candidates.push(['py', ['-3']]);

  for (const [cmd, args] of candidates) {
    if (!which(cmd)) continue;
    const r = await run(cmd, [...args, '-c', 'import sys;print("%d.%d" % sys.version_info[:2])'],
      { timeoutMs: 15000 });
    if (r.code !== 0) continue; // includes the Microsoft Store "python" stub
    const m = /^(\d+)\.(\d+)$/.exec(r.lines[r.lines.length - 1] || '');
    if (m && Number(m[1]) === 3 && Number(m[2]) >= 12) {
      return { cmd, args, version: `${m[1]}.${m[2]}` };
    }
  }
  return null;
}

/** Locate the VS Code `code` CLI: override, PATH, then well-known install paths. */
function findCodeCli(env) {
  if (env.PROVENANCE_CODE_BIN && isFile(env.PROVENANCE_CODE_BIN)) return env.PROVENANCE_CODE_BIN;
  const onPath = which('code');
  if (onPath) return onPath;
  const known = [];
  if (IS_WIN) {
    if (process.env.LOCALAPPDATA) {
      known.push(path.join(process.env.LOCALAPPDATA, 'Programs', 'Microsoft VS Code', 'bin', 'code.cmd'));
    }
    if (process.env.ProgramFiles) {
      known.push(path.join(process.env.ProgramFiles, 'Microsoft VS Code', 'bin', 'code.cmd'));
    }
  } else if (IS_MAC) {
    known.push('/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code');
  } else {
    known.push('/usr/bin/code', '/snap/bin/code', '/usr/local/bin/code');
  }
  return known.find(isFile) || null;
}

module.exports = {
  IS_WIN, IS_MAC, StepError, sleep, which, run, runOrThrow, killTree, isPidAlive,
  waitFor, progress, httpGetJson, readJson, writeJson, walk, hashFiles, isFile, tailFile, followFile,
  parseDotenv, findPython, findCodeCli, quoteForCmd,
};
