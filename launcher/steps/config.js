'use strict';
// Step 1: configuration. Layers process env over provenance/.env (the same precedence
// python-dotenv's load_dotenv gives the Python side) and fails fast without OPENAI_API_KEY,
// before anything has been started.
//
// Deliberately NOT read: a .env in the directory you ran the command from. That belongs
// to whatever repo you are looking at, not to Provenance.

const fs = require('fs');
const { parseDotenv, StepError } = require('../util');

/** Populate ctx.env / esUrl / service / slackToken. Returns notes about where things came from. */
function applyConfig(ctx, { requireKey }) {
  const { envFile } = ctx.paths;
  const haveFile = fs.existsSync(envFile);
  const fromFile = haveFile ? parseDotenv(fs.readFileSync(envFile, 'utf8')) : {};

  const env = { ...process.env };
  for (const [k, v] of Object.entries(fromFile)) {
    if (!(k in env)) env[k] = v; // real environment variables win, like load_dotenv()
  }
  ctx.env = env;

  const key = (env.OPENAI_API_KEY || '').trim();
  if (requireKey && !key) {
    // An empty variable in the real environment shadows .env (load_dotenv doesn't override).
    const shadowed = 'OPENAI_API_KEY' in process.env && (fromFile.OPENAI_API_KEY || '').trim();
    const fileNote = !haveFile ? '(file does not exist)'
      : shadowed ? '(has a key, but the empty environment variable above overrides it)'
        : '(found, but the key is blank)';
    throw new StepError(
      [
        'OPENAI_API_KEY is not set. Places checked:',
        `  - the OPENAI_API_KEY environment variable (${'OPENAI_API_KEY' in process.env ? 'set but empty' : 'not set'})`,
        `  - ${envFile} ${fileNote}`,
      ].join('\n'),
      {
        hint: shadowed
          ? 'Remove the empty OPENAI_API_KEY from your shell environment, or set it to your key.'
          : 'Copy .env.example to .env in the repo folder and set OPENAI_API_KEY=sk-...',
      },
    );
  }

  // A Slack token pasted into the wrong line fails much later with an OpenAI 401, after the
  // venv and pip install. Catch it here. Only the prefix is ever reported, never the value.
  if (requireKey && /^xox[a-z]-/i.test(key)) {
    throw new StepError(
      `OPENAI_API_KEY holds a Slack token (it starts with "${key.slice(0, 5)}"), not an OpenAI key.`,
      { hint: 'Put the OpenAI key (starts with sk-) in OPENAI_API_KEY and keep the Slack token in SLACK_USER_TOKEN.' },
    );
  }

  ctx.esUrl = (env.ELASTICSEARCH_URL || 'http://localhost:9200').trim().replace(/\/+$/, '');

  // The service always binds 127.0.0.1 (it has no auth); only the port is configurable.
  let port = 8000;
  try { port = Number(new URL(env.PROVENANCE_SERVICE_URL || '').port) || 8000; } catch { /* default */ }
  ctx.service = { host: '127.0.0.1', port, url: `http://127.0.0.1:${port}` };

  ctx.slackToken = (env.SLACK_USER_TOKEN || '').trim();

  return {
    keySource: (process.env.OPENAI_API_KEY || '').trim() ? 'environment' : '.env',
    haveFile,
  };
}

module.exports = {
  id: 'config',
  title: 'Configuration',
  quiet: true,
  applyConfig,

  async check() { return { done: false }; },

  async run(ctx) {
    const { keySource, haveFile } = applyConfig(ctx, { requireKey: true });
    const source = ctx.slackToken ? 'live Slack' : 'seed demo data';
    return {
      note: `OPENAI_API_KEY set (${keySource}); Slack token ${ctx.slackToken ? 'set' : 'not set'} -> ${source}`
        + (haveFile ? '' : '; no .env file'),
    };
  },
};
