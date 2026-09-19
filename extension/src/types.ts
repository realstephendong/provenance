// Mirrors hindsight/models.py. The contract is frozen; if this drifts, the
// Python side is the source of truth.

export interface Result {
  id: string;
  channel_name: string;
  permalink: string;
  summary: string;
  why: string;
  participants: string[];
  date: string;
  match_type: "exact" | "semantic";
  score: number;
  raw_text: string;
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
}

export interface ContextResponse {
  synthesis: string | null;
  results: Result[];
  blame: BlameInfo;
  timing_ms: Record<string, number>;
  message: string | null;
}

export interface Selection {
  code: string;
  file_path: string;
  repo_root: string;
  line_start: number;
  line_end: number;
  language?: string;
}
