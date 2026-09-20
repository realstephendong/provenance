#!/usr/bin/env node
'use strict';
// provenance-start: bring up everything Provenance needs, then open VS Code.
// The work lives in ../launcher (one Step per stage); this file only wires it together.

if (Number(process.versions.node.split('.')[0]) < 20) {
  console.error(`provenance-start needs Node.js 20 or newer (found ${process.versions.node}).`);
  process.exit(1);
}

const fs = require('fs');
const path = require('path');

// Resolve through the npm-link symlink so the repo root is right from any working directory.
const repoRoot = path.resolve(path.dirname(fs.realpathSync(__filename)), '..');
const userCwd = process.cwd();

const { Logger } = require('../launcher/log');
const { buildContext } = require('../launcher/ctx');
const { runPipeline } = require('../launcher/pipeline');
const { followFile, isPidAlive } = require('../launcher/util');
const { applyConfig } = require('../launcher/steps/config');
const startSteps = require('../launcher/steps');
const stopSteps = require('../launcher/steps/stop');

const USAGE = `provenance-start - start Provenance and open VS Code on the current directory

Usage: provenance-start [options]

  (no options)  Start Elasticsearch, the Python env, ingest (first run only), the
                VS Code extension and the API service, then open VS Code here.
  --reingest    Drop and rebuild the index (spends OpenAI tokens).
  --no-open     Do everything except opening VS Code.
  --logs        Follow the API service log (Ctrl-C stops following, not the service).
  --stop        Stop the API service and the Elasticsearch container.
  -h, --help    Show this help.

Config: OPENAI_API_KEY (required) and the optional keys live in provenance/.env.`;

function parseArgs(argv) {
  const flags = {};
  for (const arg of argv) {
    switch (arg) {
      case '--reingest': flags.reingest = true; break;
      case '--no-open': flags.noOpen = true; break;
      case '--stop': flags.stop = true; break;
      case '--logs': flags.logs = true; break;
      case '-h': case '--help': flags.help = true; break;
      default:
        console.error(`Unknown option: ${arg}\n\n${USAGE}`);
        process.exit(2);
    }
  }
  return flags;
}

/** One launcher at a time: two racing on venv creation, ingest or the service would corrupt state. */
function acquireLock(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  try {
    fs.writeFileSync(file, String(process.pid), { flag: 'wx' });
  } catch (err) {
    if (err.code !== 'EEXIST') throw err;
    const other = parseInt(fs.readFileSync(file, 'utf8'), 10);
    if (other && other !== process.pid && isPidAlive(other)) return other;
    fs.writeFileSync(file, String(process.pid)); // stale lock
  }
  process.on('exit', () => { try { fs.unlinkSync(file); } catch { /* already gone */ } });
  return null;
}

async function main() {
  const flags = parseArgs(process.argv.slice(2));
  if (flags.help) { console.log(USAGE); return 0; }

  const log = new Logger();
  const ctx = buildContext({ repoRoot, userCwd, flags, log });
  log.attach(ctx.paths.launcherLog);

  if (flags.logs) {
    console.log(`Following ${ctx.paths.serviceLog} (Ctrl-C to stop following)\n`);
    await followFile(ctx.paths.serviceLog, { onLine: (l) => console.log(l) });
    return 0;
  }

  const holder = acquireLock(ctx.paths.launcherLock);
  if (holder) {
    log.error(`Another provenance-start is already running (pid ${holder}).`);
    return 1;
  }

  if (flags.stop) {
    log.banner('provenance-start --stop');
    applyConfig(ctx, { requireKey: false });
    const ok = await runPipeline(stopSteps, ctx, { summary: false });
    return ok ? 0 : 1;
  }

  log.banner('provenance-start');
  log.info(`repo: ${repoRoot}`);
  if (!flags.noOpen) log.info(`will open: ${userCwd}`);
  log.blank();
  return (await runPipeline(startSteps, ctx)) ? 0 : 1;
}

process.on('SIGINT', () => {
  console.log('\ninterrupted');
  process.exit(130); // the 'exit' handler releases the lock
});

main().then(
  (code) => {
    process.exitCode = code;
    // Safety net in case a keep-alive socket holds the event loop open.
    setTimeout(() => process.exit(code), 1500).unref();
  },
  (err) => {
    console.error(err && err.stack ? err.stack : err);
    process.exit(1);
  },
);
