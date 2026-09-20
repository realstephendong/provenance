"""The frozen contract. Ingest, the live service, the extension, the CLI, and the
MCP server all agree on these shapes. Change only by updating every consumer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

MatchType = Literal["exact", "semantic"]
EdgeType = Literal[
    "CREATED_BY", "PART_OF", "AUTHORED_BY", "DISCUSSED_IN",
    "TRACKED_BY", "RELATED_TO", "REFERENCES", "CONFLICTS_WITH", "SUPERSEDES",
]
NodeType = Literal[
    "Code", "Commit", "PullRequest", "SlackThread", "Ticket", "SentryIssue", "Person",
]
Confidence = Literal["exact", "llm-flagged"]

# --- POST /context ----------------------------------------------------------


class ContextRequest(BaseModel):
    code: str
    file_path: str          # repo-relative
    repo_root: str
    line_start: int          # 1-indexed, inclusive
    line_end: int             # 1-indexed, inclusive
    language: str | None = None
    # Optional because CLI/MCP callers may still use the configured repository.
    # The extension supplies this from the workspace's origin remote so a central
    # GitHub App can select the matching installation without per-repo env vars.
    github_repo: str = ""          # "owner/repo"
    precomputed_blame: dict | None = None

    @field_validator("line_end")
    @classmethod
    def end_after_start(cls, v: int, info) -> int:
        start = info.data.get("line_start", 1)
        if v < start:
            raise ValueError("line_end must be >= line_start")
        return v

    @field_validator("line_start")
    @classmethod
    def start_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("line_start must be >= 1")
        return v


class Result(BaseModel):
    id: str
    channel_name: str
    permalink: str
    summary: str
    why: str
    participants: list[str] = Field(default_factory=list)
    date: str
    match_type: MatchType
    score: float
    raw_text: str = ""


class CommitInfo(BaseModel):
    """One commit in the selected range's history.

    Two kinds live here. A `current=True` commit still owns at least one line of the
    selection, which is what `git blame` reports. A `current=False` commit touched
    those lines at some point and was overwritten since, which only `git log -L`
    knows -- it is the PR whose decision the code no longer reflects, and it is
    exactly the one whose Slack thread would otherwise read as a live constraint.
    """

    sha: str                        # short (7)
    author: str | None = None
    date: str | None = None         # YYYY-MM-DD
    ts: float | None = None
    lines: int = 0                  # lines of the selection this commit owns *now*
    pr_number: int | None = None
    dominant: bool = False          # owns the most lines of the range
    current: bool = True            # False = superseded by a later commit in the chain


class BlameInfo(BaseModel):
    authors: list[str] = Field(default_factory=list)
    dominant_sha: str | None = None
    all_shas: list[str] = Field(default_factory=list)
    pr_number: int | None = None
    pr_numbers: list[int] = Field(default_factory=list)
    commit_date: str | None = None
    commit_ts: float | None = None
    uncommitted: bool = False
    # Per-commit detail, oldest first: the range's story in the order it happened,
    # which is the order every surface renders. The scalar fields above still
    # describe the *dominant* commit -- they anchor retrieval's time decay and the
    # "Origin" line the CLI, the MCP server and the extension header print, and
    # neither of those means "the oldest thing that ever touched this".
    commits: list[CommitInfo] = Field(default_factory=list)


class GraphNode(BaseModel):
    id: str                  # e.g. "commit:130e156", "pr:4821", "slack:C01/1706438420.000100"
    type: NodeType
    label: str
    data: dict = Field(default_factory=dict)


class GraphEdge(BaseModel):
    source: str
    target: str
    type: EdgeType
    confidence: Confidence


class Graph(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class ConflictPair(BaseModel):
    a: int                    # citation number in the synthesis, 1-indexed
    b: int
    kind: Literal["conflict", "supersede"]


class ContextResponse(BaseModel):
    synthesis: str | None = None
    results: list[Result] = Field(default_factory=list)
    blame: BlameInfo = Field(default_factory=BlameInfo)
    graph: Graph = Field(default_factory=Graph)
    conflicts: list[ConflictPair] = Field(default_factory=list)
    timing_ms: dict[str, int] = Field(default_factory=dict)
    message: str | None = None
    # set on a recoverable pipeline error (18); message stays user-facing, error is diagnostic
    error: str | None = None


class CountResponse(BaseModel):
    count: int
    has_exact: bool


# --- Internal: what one Elasticsearch document carries -----------------------


class ThreadPayload(BaseModel):
    thread_id: str
    channel_id: str
    channel_name: str
    channel_tier: int
    permalink: str
    ts_start: float
    ts_end: float
    participants: list[str] = Field(default_factory=list)
    message_count: int = 0
    summary: str = ""
    raw_text: str = ""
    pr_refs: list[int] = Field(default_factory=list)
    commit_shas: list[str] = Field(default_factory=list)
    ticket_refs: list[str] = Field(default_factory=list)
    file_paths: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    reactions: list[str] = Field(default_factory=list)
    is_bookmarked: bool = False
    content_hash: str = ""     # sha256 of raw_text; used by --mode reconcile
