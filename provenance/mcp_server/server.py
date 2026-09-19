"""MCP stdio server: lets a coding agent pull team context mid-task.

One tool, wrapping the same `POST /context` the extension and CLI use. There is no
retrieval logic here and there never will be (18 row 32) -- this process is a
renderer, turning the shared ContextResponse into markdown, because agents read prose
better than JSON and it pastes straight into a prompt.

Naming note: the SDK renamed `FastMCP` to `MCPServer` in mcp 2.x, which is what this
targets. The package directory is `provenance/mcp_server/` rather than
`provenance/mcp/` so it can never shadow the `mcp` PyPI package on the import path.
"""

from __future__ import annotations

import sys

import httpx
from mcp.server.mcpserver import MCPServer

from .. import config, observability

SERVICE_TIMEOUT_SECONDS = 60

server = MCPServer(
    name="provenance",
    instructions=(
        "Recovers the evidence chain behind a piece of code: the commit that wrote "
        "it, the PR it belongs to, the Slack threads that discuss it, the ticket "
        "that tracked it, and any incident it caused."
    ),
)


def _render(data: dict) -> str:
    """ContextResponse JSON -> markdown."""
    out: list[str] = []

    blame = data.get("blame") or {}
    if blame.get("dominant_sha"):
        authors = ", ".join(blame.get("authors") or []) or "unknown"
        line = f"**Origin**: commit `{blame['dominant_sha']}` by {authors}"
        if blame.get("commit_date"):
            line += f" on {blame['commit_date']}"
        prs = blame.get("pr_numbers") or []
        if prs:
            line += " — " + ", ".join(f"PR #{p}" for p in prs)
        out.append(line)
    elif blame.get("uncommitted"):
        out.append("**Origin**: these lines are not committed yet.")

    if data.get("synthesis"):
        out.append(f"\n**Why this code is the way it is**\n\n{data['synthesis']}")

    results = data.get("results") or []
    if results:
        out.append("\n**Evidence**")
        for i, r in enumerate(results, start=1):
            tag = "exact match" if r.get("match_type") == "exact" else "semantic match"
            people = ", ".join(r.get("participants") or []) or "unknown"
            out.append(
                f"\n[{i}] #{r.get('channel_name', '?')} — {r.get('date', '')} ({tag})\n"
                f"    {r.get('summary', '')}\n"
                f"    who: {people}\n"
                f"    why it matched: {r.get('why') or 'n/a'}\n"
                f"    link: {r.get('permalink', '')}"
            )

    conflicts = data.get("conflicts") or []
    if conflicts:
        out.append("")
        for c in conflicts:
            a, b = c.get("a"), c.get("b")
            if c.get("kind") == "supersede":
                out.append(
                    f"Note: [{b}] was superseded by [{a}] — treat [{a}] as current."
                )
            else:
                out.append(
                    f"Note: [{a}] and [{b}] disagree — the merged PR is ground truth."
                )

    graph = data.get("graph") or {}
    tickets = [n for n in graph.get("nodes", []) if n.get("type") == "Ticket"]
    incidents = [n for n in graph.get("nodes", []) if n.get("type") == "SentryIssue"]
    if tickets or incidents:
        out.append("")
        for n in tickets:
            out.append(f"Ticket {n['label']}: {n.get('data', {}).get('title', '')}")
        for n in incidents:
            out.append(f"Incident {n['label']}: {n.get('data', {}).get('title', '')}")

    if not results and not data.get("synthesis"):
        out.append(data.get("message") or "No relevant team context found for this code.")
        if not blame.get("dominant_sha"):
            return "\n".join(out)

    out.append(
        "\nTreat this evidence as constraints: a change that ignores it is likely a "
        "regression."
    )
    return "\n".join(out)


@server.tool()
async def search_team_context(
    code: str,
    file_path: str,
    repo_root: str,
    line_start: int = 1,
    line_end: int = 1,
) -> str:
    """Find Slack discussions, PRs, tickets, and incidents that explain a piece of code.

    Use this before modifying unfamiliar code, especially code with hardcoded values,
    unusual special cases, retry or timeout constants, or comments referencing incidents.
    Returns the evidence chain behind the code -- including any conflicting proposals
    that were considered and rejected -- so a change can respect the original constraint.
    """
    payload = {
        "code": code,
        "file_path": file_path,
        "repo_root": repo_root,
        "line_start": max(1, line_start),
        "line_end": max(line_start, line_end),
    }

    # A single span, so an agent-initiated request is distinguishable in Sentry from
    # a human-initiated one by its entry span. The service still emits the same six
    # inner spans either way.
    with observability.span("mcp.tool_call", "search_team_context"):
        try:
            async with httpx.AsyncClient(timeout=SERVICE_TIMEOUT_SECONDS) as http:
                resp = await http.post(f"{config.SERVICE_URL}/context", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            # 18 row 31: an MCP tool failure must never break the calling agent's turn.
            return f"Provenance is unavailable ({exc}). Proceed without team context."

    return _render(data)


def main() -> None:
    try:
        config.require_api_key()
    except config.MissingAPIKey as exc:
        sys.exit(str(exc))
    observability.init()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
