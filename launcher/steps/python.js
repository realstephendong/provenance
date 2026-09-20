'use strict';
// Step 3: the Python virtualenv. Creates .venv if missing and runs pip install only when
// requirements.txt has changed since the last successful install.

const {
  StepError, isFile, hashFiles, findPython, runOrThrow,
} = require('../util');

// pip is chatty; show progress and problems, not every download bar.
const pipLine = (log) => (line) => {
  if (/^(Collecting|Installing collected|Successfully|ERROR|WARNING)/.test(line)) log.detail(line);
};

module.exports = {
  id: 'python',
  title: 'Python environment',

  async check(ctx) {
    const hash = hashFiles([ctx.paths.requirements], ctx.repoRoot);
    const haveVenv = isFile(ctx.paths.venvPython);
    if (haveVenv && ctx.state.requirementsHash === hash) {
      return { done: true, note: '.venv ready, requirements unchanged' };
    }
    return {
      done: false,
      hash,
      note: haveVenv ? 'requirements changed, installing' : 'creating .venv and installing requirements',
    };
  },

  async run(ctx, checked) {
    const { log } = ctx;
    if (!isFile(ctx.paths.venvPython)) {
      const py = await findPython(ctx.env);
      if (!py) {
        throw new StepError('Python 3.12 or newer was not found', {
          hint: 'Install Python 3.12+ (https://www.python.org/downloads/) or set PROVENANCE_PYTHON to its path.',
        });
      }
      log.detail(`using Python ${py.version} (${[py.cmd, ...py.args].join(' ')})`);
      await runOrThrow(py.cmd, [...py.args, '-m', 'venv', ctx.paths.venvDir], ctx.child(),
        'Creating the virtualenv');
    }

    await runOrThrow(
      ctx.paths.venvPython,
      ['-m', 'pip', 'install', '--disable-pip-version-check', '-r', ctx.paths.requirements],
      ctx.child({ onLine: pipLine(log) }),
      'pip install',
      'Fix the error above, or delete .venv and retry.',
    );
    ctx.saveState({ requirementsHash: checked.hash }); // only after a successful install
    return { note: 'dependencies installed' };
  },
};
