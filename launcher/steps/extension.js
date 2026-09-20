'use strict';
// Step 5: build the VS Code extension into a .vsix and install it. Rebuilds only when a
// content hash of extension/src, package.json and tsconfig.json changed; reinstalls only
// when the installed copy is not the one that hash produced.

const fs = require('fs');
const path = require('path');
const {
  StepError, isFile, walk, hashFiles, readJson, findCodeCli, run, runOrThrow,
} = require('../util');

const npmLine = (log) => (line) => {
  if (/^(added|up to date|npm (error|warn)|>|.*(error|Error) TS)/.test(line) || /DONE|Packaged/.test(line)) {
    log.detail(line);
  }
};

function sourceHash(ctx) {
  const dir = ctx.paths.extensionDir;
  const files = walk(path.join(dir, 'src'));
  files.push(path.join(dir, 'package.json'), path.join(dir, 'tsconfig.json'));
  return hashFiles(files, dir);
}

async function installedExtensions(codeBin, env) {
  const r = await run(codeBin, ['--list-extensions', '--show-versions'], { env, timeoutMs: 45000 });
  return r.code === 0 ? r.lines.map((l) => l.trim().toLowerCase()) : null;
}

module.exports = {
  id: 'extension',
  title: 'VS Code extension',

  async check(ctx) {
    ctx.codeBin = ctx.codeBin || findCodeCli(ctx.env);
    if (!ctx.codeBin) {
      throw new StepError('The VS Code "code" command was not found', {
        hint: 'In VS Code run "Shell Command: Install \'code\' command in PATH", '
          + 'or set PROVENANCE_CODE_BIN to the full path of the code launcher.',
      });
    }

    const pkg = readJson(path.join(ctx.paths.extensionDir, 'package.json'), {});
    const id = `${pkg.publisher}.${pkg.name}`.toLowerCase();
    const hash = sourceHash(ctx);
    const built = isFile(ctx.paths.vsix) && ctx.state.extensionHash === hash;

    const list = await installedExtensions(ctx.codeBin, ctx.env);
    const installed = Boolean(list && list.some((l) => l.startsWith(`${id}@`)))
      && ctx.state.installedHash === hash;

    ctx.addSummary('Extension', `${id}@${pkg.version}`);
    if (built && installed) return { done: true, note: `${id}@${pkg.version} installed, up to date` };
    return {
      done: false,
      hash,
      built,
      note: built ? 'installing into VS Code' : 'building and installing',
    };
  },

  async run(ctx, checked) {
    const { log, paths } = ctx;
    const extDir = paths.extensionDir;
    const opts = ctx.child({ cwd: extDir, onLine: npmLine(log) });

    if (!checked.built) {
      const haveDeps = isFile(path.join(extDir, 'node_modules', 'typescript', 'package.json'))
        && isFile(path.join(extDir, 'node_modules', '@vscode', 'vsce', 'package.json'));
      if (!haveDeps) {
        await runOrThrow('npm', ['install', '--no-audit', '--no-fund'], opts, 'npm install',
          'Check your network connection and that Node.js/npm are installed.');
      }
      await runOrThrow('npm', ['run', 'compile'], opts, 'Compiling the extension (tsc)');
      await runOrThrow('npm', ['run', 'package'], opts, 'Packaging the extension (vsce)');
      if (!fs.existsSync(paths.vsix)) {
        throw new StepError(`vsce finished but ${paths.vsix} was not created`);
      }
      ctx.saveState({ extensionHash: checked.hash });
    }

    await runOrThrow(ctx.codeBin, ['--install-extension', paths.vsix, '--force'],
      ctx.child({ cwd: ctx.repoRoot }), 'code --install-extension');
    ctx.saveState({ installedHash: checked.hash });
    return { note: 'installed; reload open VS Code windows to pick it up' };
  },
};
