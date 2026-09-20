// TypeScript mirror of provenance/models.py's public shapes.
// Python is the source of truth. If these drift, fix this side.

export type MatchType = 'exact' | 'semantic';
export type Confidence = 'exact' | 'llm-flagged';

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
