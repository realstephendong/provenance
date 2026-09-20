import { ChildProcess, spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import * as vscode from 'vscode';
import { ContextResponse, LocalBackfillResult, LocalStatus, Selection } from './types';

/**
 * Supervises the private connector: a separate process, on this machine, holding
 * this person's Slack token and their encrypted local index.
 *
 * Why a separate process rather than doing it in the extension host: secrets stay
 * out of a process that loads arbitrary other extensions' code, indexing survives a
 * window reload, and a wedged embedding call cannot freeze the editor. The cost is
 * that the connector must be installed — so it is opt-in, and when it is missing the
 * extension keeps working with shared results only rather than nagging.
 *
 * The connector is addressed by a port it chooses and announces on stdout, and
 * authenticated by a secret this class generates per launch and passes through the
 * environment. The secret is never written to disk and never passed as an argument,
 * where it would show up in the process list for every user on the machine.
 */

const HANDSHAKE_TIMEOUT_MS = 20_000;
const REQUEST_TIMEOUT_MS = 60_000;
const INDEX_TIMEOUT_MS = 180_000;
// Restarting forever would hide a genuinely broken install behind an endless
// spawn loop; this many failures and it stays down until asked again.
const MAX_RESTARTS = 3;

export class LocalAgentUnavailable extends Error {}

interface Handshake {
  port: number;
  pid: number;
  profile: string;
}

function settings() {
  const cfg = vscode.workspace.getConfiguration('provenance');
  return {
    enabled: cfg.get<boolean>('local.enabled', false),
    command: cfg.get<string[]>('local.command', []),
    python: cfg.get<string>('local.pythonPath', 'python3'),
    profile: cfg.get<string>('local.profile', 'default'),
  };
}

export class LocalAgent implements vscode.Disposable {
  private child: ChildProcess | undefined;
  private handshake: Handshake | undefined;
  private starting: Promise<Handshake> | undefined;
  private secret = '';
  private restarts = 0;
  private stopped = false;
  private readonly output: vscode.OutputChannel;
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changed.event;

  constructor() {
    this.output = vscode.window.createOutputChannel('Provenance (private)');
  }

  get enabled(): boolean {
    return settings().enabled;
  }

  get running(): boolean {
    return Boolean(this.handshake && this.child && !this.child.killed);
  }

  /** Start if enabled and not already running. Never throws; failures are reported. */
  async ensure(): Promise<boolean> {
    if (!this.enabled || this.stopped) { return false; }
    if (this.running) { return true; }
    try {
      await this.start();
      return true;
    } catch (err) {
      this.output.appendLine(`could not start the private connector: ${String(err)}`);
      return false;
    }
  }

  private start(): Promise<Handshake> {
    if (this.starting) { return this.starting; }
    this.starting = new Promise<Handshake>((resolve, reject) => {
      const { command, python, profile } = settings();
      const argv = command.length > 0
        ? command
        : [python, '-m', 'provenance.local_agent.main', 'serve', '--profile', profile];
      this.secret = randomBytes(32).toString('base64url');

      this.output.appendLine(`starting: ${argv.join(' ')}`);
      const child = spawn(argv[0], argv.slice(1), {
        env: { ...process.env, PROVENANCE_LOCAL_SECRET: this.secret },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      this.child = child;

      const timer = setTimeout(() => {
        reject(new Error('the connector did not report a port in time'));
        child.kill();
      }, HANDSHAKE_TIMEOUT_MS);

      let buffered = '';
      child.stdout?.on('data', (chunk: Buffer) => {
        buffered += chunk.toString();
        let newline = buffered.indexOf('\n');
        while (newline >= 0) {
          const line = buffered.slice(0, newline).trim();
          buffered = buffered.slice(newline + 1);
          newline = buffered.indexOf('\n');
          if (!line) { continue; }
          try {
            const parsed = JSON.parse(line) as { provenance_local?: Handshake };
            if (parsed.provenance_local?.port) {
              clearTimeout(timer);
              this.handshake = parsed.provenance_local;
              this.restarts = 0;
              this.output.appendLine(`ready on 127.0.0.1:${this.handshake.port}`);
              this.changed.fire();
              resolve(this.handshake);
              continue;
            }
          } catch {
            // Not the handshake — the connector's own logging. Keep it visible.
          }
          this.output.appendLine(line);
        }
      });

      child.stderr?.on('data', (chunk: Buffer) => this.output.append(chunk.toString()));

      child.on('error', (err) => {
        clearTimeout(timer);
        reject(new Error(
          `${argv[0]} could not be started (${err.message}). Install the connector ` +
          '(pip install provenance) or set provenance.local.command.',
        ));
      });

      child.on('exit', (code, signal) => {
        clearTimeout(timer);
        this.handshake = undefined;
        this.child = undefined;
        this.starting = undefined;
        this.changed.fire();
        if (this.stopped) { return; }
        this.output.appendLine(`connector exited (code ${code ?? 'null'}, signal ${signal ?? 'none'})`);
        if (this.restarts < MAX_RESTARTS) {
          this.restarts += 1;
          setTimeout(() => void this.ensure(), 1000 * this.restarts);
        } else {
          this.output.appendLine('giving up after repeated failures; private results are off');
        }
      });
    }).finally(() => { this.starting = undefined; });
    return this.starting;
  }

  stop(): void {
    this.stopped = true;
    this.child?.kill();
    this.child = undefined;
    this.handshake = undefined;
    this.changed.fire();
  }

  /** Allow a deliberate restart after the extension gave up, or after enabling it. */
  resume(): void {
    this.stopped = false;
    this.restarts = 0;
  }

  // --- requests --------------------------------------------------------------

  private async request<T>(method: string, path: string, body?: unknown,
                           timeoutMs = REQUEST_TIMEOUT_MS): Promise<T> {
    if (!await this.ensure()) {
      throw new LocalAgentUnavailable('the private connector is not running');
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`http://127.0.0.1:${this.handshake!.port}${path}`, {
        method,
        headers: {
          'Content-Type': 'application/json',
          'X-Provenance-Local-Secret': this.secret,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      const text = await response.text();
      const parsed = text ? JSON.parse(text) : {};
      if (!response.ok) {
        // 428 is the connector asking for consent before it processes private text.
        // It has to reach the caller intact so the UI can ask rather than just fail.
        const error = new Error(String(parsed.detail ?? `${response.status} ${response.statusText}`));
        (error as Error & { status?: number }).status = response.status;
        throw error;
      }
      return parsed as T;
    } finally {
      clearTimeout(timer);
    }
  }

  status(): Promise<LocalStatus> {
    return this.request<LocalStatus>('GET', '/v1/status');
  }

  context(selection: Selection): Promise<ContextResponse> {
    return this.request<ContextResponse>('POST', '/v1/context', selection);
  }

  /** The private half of a Backfill press: this account's private channels, here. */
  backfill(): Promise<LocalBackfillResult> {
    return this.request<LocalBackfillResult>('POST', '/v1/backfill', undefined,
                                             INDEX_TIMEOUT_MS);
  }

  startSlackAuth(): Promise<{ authorize_url: string; state: string }> {
    return this.request('POST', '/v1/auth/slack/start');
  }

  importSlackToken(token: string): Promise<{ signed_in: boolean; team_name?: string }> {
    return this.request('POST', '/v1/auth/slack/token', { token });
  }

  revokeSlack(): Promise<{ signed_in: boolean; revoked_at_slack: boolean }> {
    return this.request('POST', '/v1/auth/slack/revoke');
  }

  setConsent(grant: boolean): Promise<{ granted: boolean }> {
    return this.request('POST', '/v1/consent', { grant });
  }

  forgetChannel(channelId: string): Promise<{ deleted: number }> {
    return this.request('DELETE', `/v1/channels/${encodeURIComponent(channelId)}`);
  }

  purge(): Promise<{ deleted_documents: number; deleted_channels: number }> {
    return this.request('DELETE', '/v1/data');
  }

  show(): void {
    this.output.show(true);
  }

  dispose(): void {
    this.stop();
    this.changed.dispose();
    this.output.dispose();
  }
}
