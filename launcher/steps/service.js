'use strict';
// Step 6: the FastAPI service. Reused if a Provenance /health already answers; otherwise
// started detached (uvicorn, bound to 127.0.0.1) with output going to .run/service.log and
// its PID to .run/service.pid. verify() waits for /health to report ok:true.

const fs = require('fs');
const { spawn } = require('child_process');
const {
  StepError, isPidAlive, waitFor, progress, tailFile, isFile,
} = require('../util');
const { probeService, readPid } = require('../services');

const HEALTH_TIMEOUT_MS = 90_000;

function startedBeforeEnvChanged(ctx) {
  try {
    return fs.statSync(ctx.paths.envFile).mtimeMs > fs.statSync(ctx.paths.servicePid).mtimeMs;
  } catch {
    return false; // no .env or no pid file: nothing to compare
  }
}

/** Why /health is not ok, in words a person can act on. */
function explainUnhealthy(body) {
  if (!body) return { message: 'The service did not answer /health in time.', hint: 'See .run/service.log.' };
  if (body.error) {
    return { message: `Service is up but unhealthy: ${body.error}`, hint: 'Is Elasticsearch reachable at the URL in .env?' };
  }
  if (body.index_exists === false) {
    return {
      message: 'Service is up but the slack_threads index does not exist.',
      hint: 'Run provenance-start --reingest to build it.',
    };
  }
  if (body.embedder_id !== body.expected_embedder_id) {
    return {
      message: `Index was built with a different embedder (${body.embedder_id}, expected ${body.expected_embedder_id}).`,
      hint: 'Run provenance-start --reingest to rebuild the index.',
    };
  }
  return { message: 'Service is up but /health is not ok.', hint: 'See .run/service.log.' };
}

module.exports = {
  id: 'service',
  title: 'API service',

  async check(ctx) {
    const probe = await probeService(ctx.service.url);
    if (probe.kind === 'other') {
      throw new StepError(`Port ${ctx.service.port} is in use by something that is not the Provenance service`, {
        hint: `Free the port, or set PROVENANCE_SERVICE_URL (and the extension's provenance.serviceUrl) to another one.`,
      });
    }
    const pid = readPid(ctx.paths.servicePid);
    if (probe.kind === 'provenance' && probe.ok) {
      ctx.addSummary('Service', `${ctx.service.url}${pid ? ` (pid ${pid})` : ''}`);
      ctx.addSummary('Service log', ctx.paths.serviceLog);
      const stale = startedBeforeEnvChanged(ctx);
      return {
        done: true,
        status: stale ? 'warn' : undefined,
        note: `already running${pid ? ` (pid ${pid})` : ''}`
          + (stale ? '; .env changed since it started - run provenance-start --stop, then start again' : ''),
      };
    }
    return {
      done: false,
      reuse: probe.kind === 'provenance',
      note: probe.kind === 'provenance'
        ? 'running but not healthy yet'
        : `starting uvicorn on ${ctx.service.host}:${ctx.service.port}`,
    };
  },

  async run(ctx, checked) {
    if (checked.reuse) return { note: 'already running' };
    const { paths, service } = ctx;

    if (!isFile(paths.venvPython)) {
      throw new StepError('The Python virtualenv is missing', { hint: 'Delete .run/state.json and retry.' });
    }

    fs.mkdirSync(paths.runDir, { recursive: true });
    try { fs.copyFileSync(paths.serviceLog, `${paths.serviceLog}.1`); } catch { /* first run */ }
    const out = fs.openSync(paths.serviceLog, 'w');

    let spawnError = null;
    const child = spawn(
      paths.venvPython,
      ['-m', 'uvicorn', 'provenance.service.main:app', '--host', service.host, '--port', String(service.port)],
      {
        cwd: ctx.repoRoot,
        env: { ...ctx.env, PYTHONUNBUFFERED: '1' },
        detached: true,
        stdio: ['ignore', out, out],
        windowsHide: true,
      },
    );
    child.once('error', (e) => { spawnError = e; });
    child.unref();
    fs.closeSync(out);

    await new Promise((r) => setTimeout(r, 300)); // let a spawn error surface
    if (spawnError || !child.pid) {
      throw new StepError(`Could not start the service: ${spawnError ? spawnError.message : 'no pid'}`);
    }
    fs.writeFileSync(paths.servicePid, String(child.pid));
    ctx.spawnedPid = child.pid;
    return { note: `started (pid ${child.pid})` };
  },

  async verify(ctx) {
    let last = null;
    try {
      const healthy = await waitFor(
        async () => {
          const p = await probeService(ctx.service.url);
          last = p.kind === 'provenance' ? p.body : last;
          return p.kind === 'provenance' && p.ok ? p : null;
        },
        {
          timeoutMs: HEALTH_TIMEOUT_MS,
          intervalMs: 1000,
          onTick: progress(ctx.log, 'waiting for the service', 10000),
          abortIf: () => (ctx.spawnedPid && !isPidAlive(ctx.spawnedPid)
            ? 'The service process exited during startup.' : null),
        },
      );
      if (!healthy) {
        const { message, hint } = explainUnhealthy(last);
        throw new StepError(message, { hint, logTail: tailFile(ctx.paths.serviceLog, 15) });
      }
    } catch (err) {
      if (err instanceof StepError && !err.logTail) {
        err.logTail = tailFile(ctx.paths.serviceLog, 15);
        err.hint = err.hint || 'See .run/service.log for the full output.';
      }
      throw err;
    }
    const pid = readPid(ctx.paths.servicePid);
    ctx.addSummary('Service', `${ctx.service.url}${pid ? ` (pid ${pid})` : ''}`);
    ctx.addSummary('Service log', ctx.paths.serviceLog);
  },
};
