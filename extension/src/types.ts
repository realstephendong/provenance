// TypeScript mirror of provenance/models.py's public shapes.
// Python is the source of truth. If these drift, fix this side.

export type MatchType = 'exact' | 'semantic';
export type Confidence = 'exact' | 'llm-flagged';

/**
 * Which half of a Backfill press produced this.
 *
 * `workspace_shared` is the company index every teammate can search.
 * `user_private` is a private channel indexed on this machine, which nobody else can
 * retrieve. Rendered, not inferred: "who else can see this?" is the first thing
 * someone needs to know about a quoted conversation, and a channel name does not
 * answer it.
 */
export type RetrievalScope = 'workspace_shared' | 'user_private';

export type NodeType =
  | 'Code' | 'Commit' | 'PullRequest' | 'SlackThread'
  | 'Ticket' | 'SentryIssue' | 'Person';

export type EdgeType =
  | 'CREATED_BY' | 'PART_OF' | 'AUTHORED_BY' | 'DISCUSSED_IN'
  | 'TRACKED_BY' | 'RELATED_TO' | 'REFERENCES' | 'CONFLICTS_WITH' | 'SUPERSEDES';

export interface ContextRequest {
  code: string;
  file_path: string;
  repo_root: string;
  line_start: number;
  line_end: number;
  language?: string | null;
  github_repo?: string;
  precomputed_blame?: Record<string, unknown>;
}

export interface Result {
  id: string;
  channel_name: string;
  permalink: string;
  summary: string;
  why: string;
  participants: string[];
  date: string;
  match_type: MatchType;
  score: number;
  raw_text: string;
  scope: RetrievalScope;
  display_scope: string;
}

export interface CommitInfo {
  sha: string;
  author: string | null;
  date: string | null;
  ts: number | null;
  lines: number;
  pr_number: number | null;
  dominant: boolean;
  /** False = these lines were overwritten by a later commit in the chain. */
  current: boolean;
}

export interface BlameInfo {
  authors: string[];
  dominant_sha: string | null;
  all_shas: string[];
  pr_number: number | null;
  pr_numbers: number[];
  commit_date: string | null;
  commit_ts: number | null;
  uncommitted: boolean;
  commits: CommitInfo[];
}

export interface GraphNode {
  id: string;
  type: NodeType;
  label: string;
  data: Record<string, unknown>;
  scope: RetrievalScope;
  display_scope: string;
}

export interface GraphEdge {
  source: string;
  target: string;
  type: EdgeType;
  confidence: Confidence;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface ConflictPair {
  a: number;
  b: number;
  kind: 'conflict' | 'supersede';
}

export interface ContextResponse {
  synthesis: string | null;
  results: Result[];
  blame: BlameInfo;
  graph: Graph;
  conflicts: ConflictPair[];
  timing_ms: Record<string, number>;
  message: string | null;
  error: string | null;
}

export interface CountResponse {
  count: number;
  has_exact: boolean;
}

/** What the editor hands the backend: a selection, already 1-indexed. */
export interface Selection {
  code: string;
  file_path: string;
  repo_root: string;
  line_start: number;
  line_end: number;
  language?: string;
  github_repo?: string;
  precomputed_blame?: Record<string, unknown>;
}

/** `GET /ingest/status` -- how far the index is caught up. Cheap; never hits Slack. */
export interface IngestStatus {
  /** Per channel, the timestamp of the newest message already indexed. */
  channels: Record<string, number>;
  /** The oldest of those: a sync is only complete through the laggard channel. */
  covered_through: number | null;
  never_run: boolean;
  source: 'export' | 'slack';
  running: boolean;
  docs?: number;
  error?: string;
  /** `'*'` = every channel the token can read; a number = that many named channels. */
  scope?: string | number;
}

/** `POST /ingest/sync` -- what one press of Backfill actually did. */
export interface IngestResult {
  ok: boolean;
  mode: string;
  new_messages: number;
  affected: number;
  units: number;
  indexed: number;
  channels: Record<string, number>;
  covered_through: number | null;
  never_run: boolean;
  /** The progress lines the CLI would have printed, in order. */
  log: string[];
}

export interface ReconcileResult {
  ok: boolean;
  mode: 'reconcile';
  missing: number;
  changed: number;
  stale: number;
  indexed: number;
  log: string[];
}

/** `GET /v1/status` on the local connector. */
export interface LocalStatus {
  ok: boolean;
  error?: string;
  slack: {
    signed_in: boolean;
    reachable?: boolean;
    user?: string;
    team_name?: string;
    client_mode?: string;
  };
  consent: { granted: boolean; version: string; text: string };
  store: {
    documents: number;
    channels: number;
    bytes: number;
    profile_id: string;
    path: string;
    key_backend: string;
  };
  channels: { channel_id: string; channel_name: string; documents: number }[];
}

/** `POST /v1/backfill` on the local connector -- the private half of one press. */
export interface LocalBackfillResult {
  ok: boolean;
  indexed: number;
  units: number;
  messages: number;
  channels: number;
  note?: string;
  log: string[];
}

export interface LocalReconcileResult extends ReconcileResult {
  messages: number;
  units: number;
  channels: number;
}
