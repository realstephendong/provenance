"""The frozen contract.

Three tracks (ingest, retrieval, extension/MCP) meet here. Change these
shapes only by agreement -- everything else in the codebase is local.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MatchType = Literal["exact", "semantic"]


# --- POST /context --------------------------------------------------------


class ContextRequest(BaseModel):
    code: str
    file_path: str  # repo-relative
    repo_root: str
    line_start: int  # 1-indexed, inclusive
    line_end: int  # 1-indexed, inclusive
    language: str | None = None


class Result(BaseModel):
    id: str
    channel_name: str
    permalink: str
    summary: str
    why: str  # one line from the reranker; shown verbatim on the card
    participants: list[str]
    date: str  # ISO date of thread start, for the card
    match_type: MatchType
    score: float
    raw_text: str = ""  # the panel does not render it; "send to agent" does


class BlameInfo(BaseModel):
    authors: list[str] = Field(default_factory=list)
    dominant_sha: str | None = None
    all_shas: list[str] = Field(default_factory=list)
    pr_number: int | None = None
    pr_numbers: list[int] = Field(default_factory=list)
    commit_date: str | None = None  # ISO date
    commit_ts: float | None = None  # unix seconds, the time-weighting anchor
    uncommitted: bool = False  # blame returned all-zero SHAs


class ContextResponse(BaseModel):
    synthesis: str | None = None
    results: list[Result] = Field(default_factory=list)
    blame: BlameInfo = Field(default_factory=BlameInfo)
    timing_ms: dict[str, int] = Field(default_factory=dict)
    message: str | None = None  # set when results are empty and why


# --- Internal: what one Qdrant point carries ------------------------------


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
