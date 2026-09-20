'use strict';
// The runner. A pipeline is an ordered array of Steps:
//
//   { id, title, quiet?,
//     check(ctx)  -> { done, note?, status? }   cheap and side-effect free; done => skip
//     run(ctx, checked) -> { note?, status? }   does the work; throws StepError on failure
//     verify?(ctx, checked)                     confirms the outcome; throws on failure }
//
// Each step owns its own idempotency in check(), so re-running the whole pipeline is safe
// and fast. The runner stops at the first failure.

async function runPipeline(steps, ctx, { summary = true } = {}) {
  const { log } = ctx;
  const started = Date.now();
  log.titleWidth = Math.max(...steps.map((s) => s.title.length));

  for (let i = 0; i < steps.length; i++) {
    const step = steps[i];
    const label = `[${i + 1}/${steps.length}]`;
    const t0 = Date.now();
    try {
      const checked = await step.check(ctx);
      if (checked.done) {
        log.step(label, step.title, checked.status === 'warn' ? 'warn' : 'skipped', checked.note);
        continue;
      }
      if (!step.quiet) log.step(label, step.title, 'running', checked.note);
      const out = (await step.run(ctx, checked)) || {};
      if (step.verify) await step.verify(ctx, checked);
      log.step(label, step.title, out.status === 'warn' ? 'warn' : 'done', out.note,
        Date.now() - t0);
    } catch (err) {
      reportFailure(ctx, label, step, err, Date.now() - t0);
      return false;
    }
  }
  if (summary) log.summary(ctx.summaryRows);
  log.info(`(${((Date.now() - started) / 1000).toFixed(1)}s)`);
  return true;
}

function reportFailure(ctx, label, step, err, elapsed) {
  const { log } = ctx;
  const [first, ...rest] = String(err.message || err).split('\n');
  log.step(label, step.title, 'fail', first, elapsed);
  for (const line of rest) log.error(line);
  if (err.logTail && err.logTail.length) {
    for (const line of err.logTail) log.detail(line);
  }
  if (err.hint) log.hint(err.hint);
  if (err.name !== 'StepError') log.error(err.stack || String(err)); // a bug, not a user error
  log.blank();
  log.info(`Full log: ${ctx.paths.launcherLog}`);
}

module.exports = { runPipeline };
