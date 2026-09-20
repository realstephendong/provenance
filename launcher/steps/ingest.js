'use strict';
// Step 4: load data into Elasticsearch. Runs once: the ingest CLI writes
// .provenance/ingest_checkpoint.json only after a backfill succeeds, so that file is the
// "already done" marker. New Slack messages after that are the Slack bot's job, not this
// step's. `--reingest` forces a full rebuild (drop + backfill).
//
// Skipped only when the checkpoint is readable AND the index has documents (a wiped
// .es_storage with a leftover checkpoint must not leave an empty index) AND the index was
// not built from seed data while a Slack token is now set.
//
//   SLACK_USER_TOKEN set  -> live Slack backfill. Failure aborts; it does NOT fall back to
//                            seed data (that would put fake threads in a real index).
//   otherwise             -> seed demo data (built first if seed/slack is missing).
//
// The index is dropped (--recreate) only when that is intended: --reingest, an empty or
// missing index, or the seed -> live Slack switch. A missing checkpoint on a non-empty
// index (e.g. the bot wrote first) backfills in place; document ids are deterministic, so
// that is an idempotent upsert.

const fs = require('fs');
const { StepError, which, run, runOrThrow, readJson } = require('../util');
const { indexCount } = require('../services');

const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

module.exports = {
  id: 'ingest',
  title: 'Slack ingest',

  async check(ctx) {
    if (ctx.flags.reingest) {
      return { done: false, recreate: true, note: '--reingest: dropping and rebuilding the index' };
    }

    const { checkpoint: checkpointFile } = ctx.paths;
    const checkpointExists = fs.existsSync(checkpointFile);
    const checkpointOk = isObject(readJson(checkpointFile, null));
    const count = await indexCount(ctx.esUrl, ctx.index);

    if (checkpointOk && count > 0) {
      if (ctx.state.ingestSource === 'seed' && ctx.slackToken) {
        return {
          done: false,
          recreate: true,
          note: 'Slack token added since the seed ingest; replacing seed data with live Slack',
        };
      }
      return { done: true, note: `checkpoint found, ${count} documents indexed` };
    }

    const why = checkpointOk ? 'index empty despite checkpoint'
      : checkpointExists ? 'checkpoint unreadable'
        : 'no checkpoint';
    // Never drop an index that has documents unless the checkpoint says we built it.
    const recreate = count === 0;
    return { done: false, recreate, note: `${why}; first-time ingest` };
  },

  async run(ctx, checked) {
    const { log, paths } = ctx;
    const live = Boolean(ctx.slackToken);
    const py = paths.venvPython;
    const recreate = checked.recreate ? ['--recreate'] : [];

    if (live) {
      log.detail('Live Slack ingest: reads the channel history and calls OpenAI (embeddings and');
      log.detail('summaries) for every thread. This spends tokens once; later runs skip it.');
      const r = await run(
        py,
        ['-m', 'provenance.ingest', '--source', 'slack', '--mode', 'backfill', ...recreate],
        ctx.child(),
      );
      if (r.code !== 0) {
        throw new StepError('Live Slack ingest failed; not falling back to seed data', {
          hint: 'Diagnose with: .venv python -m provenance.ingest.slack_check   '
            + '(or blank SLACK_USER_TOKEN in .env to use the seed demo data).',
          logTail: r.lines.slice(-10),
        });
      }
    } else {
      if (!fs.existsSync(paths.seedSlack)) {
        if (!which('git')) {
          throw new StepError('git is required to build the seed data but was not found on PATH', {
            hint: 'Install git, or set SLACK_USER_TOKEN in .env to ingest live Slack instead.',
          });
        }
        log.detail('Building seed data (seed/build_seed.py)...');
        await runOrThrow(py, [paths.buildSeed], ctx.child(), 'Building seed data');
      }
      log.detail('Ingesting seed data; this calls OpenAI (embeddings and summaries).');
      await runOrThrow(
        py,
        ['-m', 'provenance.ingest', '--export', 'seed/slack', '--mode', 'backfill', ...recreate],
        ctx.child(),
        'Seed ingest',
      );
    }

    // The ingest CLI writes the checkpoint last. If it is missing, the next start would
    // silently ingest again, so fail loudly instead.
    if (!fs.existsSync(paths.checkpoint)) {
      throw new StepError(`Ingest finished but ${paths.checkpoint} was not written`, {
        hint: 'See the ingest output above; the next start would repeat the ingest.',
      });
    }

    const count = await indexCount(ctx.esUrl, ctx.index);
    ctx.saveState({ ingestedAt: new Date().toISOString(), ingestSource: live ? 'slack' : 'seed' });
    return { note: `${count} documents indexed from ${live ? 'live Slack' : 'seed data'}` };
  },
};
