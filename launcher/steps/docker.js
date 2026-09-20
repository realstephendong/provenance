'use strict';
// Step 2: Elasticsearch, via Docker. Starts Docker Desktop if the daemon is down, runs
// `docker compose up -d`, then waits for the cluster to answer.

const path = require('path');
const { spawn } = require('child_process');
const {
  IS_WIN, IS_MAC, StepError, which, isFile, runOrThrow, waitFor, progress,
} = require('../util');
const { isLocalUrl, esHealth, dockerUp, composeCommand } = require('../services');

const DAEMON_TIMEOUT_MS = 120_000;
const ES_TIMEOUT_MS = 180_000;

function dockerDesktopExe() {
  const candidates = [];
  if (process.env.ProgramFiles) {
    candidates.push(path.join(process.env.ProgramFiles, 'Docker', 'Docker', 'Docker Desktop.exe'));
  }
  if (process.env.LOCALAPPDATA) {
    candidates.push(path.join(process.env.LOCALAPPDATA, 'Programs', 'Docker', 'Docker', 'Docker Desktop.exe'));
  }
  return candidates.find(isFile) || null;
}

async function startDockerDaemon(ctx) {
  const { log } = ctx;
  if (IS_WIN) {
    const exe = dockerDesktopExe();
    if (!exe) {
      throw new StepError('The Docker daemon is not running and Docker Desktop was not found', {
        hint: 'Start Docker Desktop manually, wait until it reports "running", then retry.',
      });
    }
    log.detail('Docker is not running; starting Docker Desktop...');
    spawn(exe, [], { detached: true, stdio: 'ignore' }).unref();
  } else if (IS_MAC) {
    log.detail('Docker is not running; starting Docker Desktop...');
    spawn('open', ['-a', 'Docker'], { detached: true, stdio: 'ignore' }).unref();
  } else {
    throw new StepError('The Docker daemon is not running', {
      hint: 'Start it, for example: sudo systemctl start docker',
    });
  }
  const up = await waitFor(dockerUp, {
    timeoutMs: DAEMON_TIMEOUT_MS,
    intervalMs: 3000,
    onTick: progress(log, 'waiting for Docker to start'),
  });
  if (!up) {
    throw new StepError(`Docker did not become ready within ${DAEMON_TIMEOUT_MS / 1000}s`, {
      hint: 'Open Docker Desktop, wait until it says it is running, then retry.',
    });
  }
}

module.exports = {
  id: 'elasticsearch',
  title: 'Elasticsearch',

  async check(ctx) {
    const health = await esHealth(ctx.esUrl);
    if (health) {
      ctx.addSummary('Elasticsearch', `${ctx.esUrl} (${health.status})`);
      return { done: true, note: `already up, status ${health.status}` };
    }
    return { done: false, note: 'starting via docker compose' };
  },

  async run(ctx) {
    if (!isLocalUrl(ctx.esUrl)) {
      throw new StepError(`Elasticsearch at ${ctx.esUrl} is not reachable`, {
        hint: 'ELASTICSEARCH_URL points to a remote host, so Docker is not started for it.',
      });
    }
    if (!which('docker')) {
      throw new StepError('Docker is not installed or not on PATH', {
        hint: 'Install Docker Desktop: https://www.docker.com/products/docker-desktop/',
      });
    }
    if (!(await dockerUp())) await startDockerDaemon(ctx);

    const compose = await composeCommand();
    await runOrThrow(
      compose.cmd,
      [...compose.args, '-f', ctx.paths.compose, 'up', '-d'],
      ctx.child(),
      'docker compose up',
    );

    const health = await waitFor(() => esHealth(ctx.esUrl), {
      timeoutMs: ES_TIMEOUT_MS,
      intervalMs: 2000,
      onTick: progress(ctx.log, 'waiting for Elasticsearch'),
    });
    if (!health) {
      throw new StepError(`Elasticsearch did not become healthy within ${ES_TIMEOUT_MS / 1000}s`, {
        hint: 'Inspect it with: docker logs provenance-es',
      });
    }
    ctx.addSummary('Elasticsearch', `${ctx.esUrl} (${health.status})`);
    return { note: `up, status ${health.status}` };
  },
};
