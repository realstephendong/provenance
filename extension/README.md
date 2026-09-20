# Provenance for VS Code

Provenance connects selected code to the commits, pull requests, Slack discussions,
and incidents that explain it.

## Install

Install the supplied `.vsix` file from VS Code's Extensions view: use the `...` menu,
choose **Install from VSIX...**, then select `provenance-1.1.0.vsix`.

## Configure

Set `provenance.serviceUrl` to your team's protected Provenance API URL. For a local
demo, use `http://127.0.0.1:8000`.

## Use

Open a Git repository, select code, and run **Provenance: Explain Selected Code**
(`Cmd+Alt+W` on macOS, `Ctrl+Alt+W` on Windows/Linux).
