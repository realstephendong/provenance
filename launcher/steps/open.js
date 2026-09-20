'use strict';
// Step 7: open VS Code on the directory provenance-start was run from (not the Provenance
// repo). Skipped with --no-open.

const path = require('path');
const { spawn } = require('child_process');
const {
  IS_WIN, StepError, which, isFile, run, runOrThrow, findCodeCli,
} = require('../util');

/** On Windows, launch Code.exe directly: no cmd.exe, so no quoting problems with the path. */
function windowsExeFor(codeBin) {
  if (!IS_WIN || !/[\\/]bin[\\/]code\.cmd$/i.test(codeBin)) return null;
  const exe = path.join(path.dirname(path.dirname(codeBin)), 'Code.exe');
  return isFile(exe) ? exe : null;
}

module.exports = {
  id: 'open',
  title: 'Open VS Code',
  quiet: true,

  async check(ctx) {
    if (ctx.flags.noOpen) return { done: true, note: 'skipped (--no-open)' };
    return { done: false };
  },

  async run(ctx) {
    const target = ctx.userCwd;
    ctx.codeBin = ctx.codeBin || findCodeCli(ctx.env);
    if (!ctx.codeBin) throw new StepError('The VS Code "code" command was not found');

    const exe = windowsExeFor(ctx.codeBin);
    if (exe) {
      spawn(exe, [target], { detached: true, stdio: 'ignore' }).unref();
    } else {
      await runOrThrow(ctx.codeBin, [target], { env: ctx.env, timeoutMs: 30000 }, 'code (open folder)');
    }

    let inRepo = true;
    if (which('git')) {
      inRepo = (await run('git', ['-C', target, 'rev-parse', '--show-toplevel'], { timeoutMs: 10000 })).code === 0;
    }
    return inRepo
      ? { note: `opened ${target}` }
      : { status: 'warn', note: `opened ${target} (not a git repository: Provenance needs one to find evidence)` };
  },
};
