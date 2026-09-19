"""Stdio MCP server wrapping the retrieval service.

The extension says "we built a sidebar". This says "we made coding agents
aware of your team's history": an agent working in an unfamiliar file calls
the tool itself, finds the constraint that explains the code, and writes a fix
that respects it.

The docstring below is the entire interface as far as the agent is concerned.
"Use this before modifying unfamiliar code" is what makes an agent reach for
it unprompted, which is the behaviour you want on stage.

Two deviations from the handoff, both forced:

  * the package is `mcp_server`, not `mcp`, so it cannot shadow the `mcp`
    PyPI package on the import path;
  * the handoff says FastMCP, which is the mcp 1.x name. On mcp 2.x that
    class is `MCPServer`. Same decorator, same stdio transport.
"""

from __future__ import annotations

import httpx
from mcp.server.mcpserver import MCPServer

from .. import config

mcp = MCPServer("hindsight")


@mcp.tool()
async def search_team_context(
    code: str,
    file_path: str,
    repo_root: str,
    line_start: int = 1,
    line_end: int = 1,
) -> str:
    """Find Slack discussions that explain a piece of code.

Use this before modifying unfamiliar code, especially code with hardcoded
values, unusual special cases, retry or timeout constants, or comments
referencing incidents. Returns the discussions that explain why the code is
the way it is, so a change can respect the original constraint.
"""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{config.SERVICE_URL}/context",
                json={
                    "code": code,
                    "file_path": file_path,
                    "repo_root": repo_root,
                    "line_start": line_start,
                    "line_end": line_end,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return f"Hindsight is unavailable ({exc}). Proceed without team context."

    return format_markdown(data, file_path, line_start, line_end)


def format_markdown(data: dict, file_path: str, line_start: int, line_end: int) -> str:
    """Markdown, not JSON. Agents read prose better, and it is what you would
    paste into a prompt anyway."""
    out: list[str] = [f"# Team context for {file_path}:{line_start}-{line_end}"]

    blame = data.get("blame") or {}
    if blame.get("dominant_sha"):
        authors = ", ".join(blame.get("authors") or []) or "unknown"
        pr = f", PR #{blame['pr_number']}" if blame.get("pr_number") else ""
        out.append(
            f"\nLast touched by {authors} on {blame.get('commit_date')} "
            f"({blame['dominant_sha']}{pr})."
        )
    elif blame.get("uncommitted"):
        out.append("\nThese lines are uncommitted, so there is no git history to anchor on.")

    if data.get("message") and not data.get("results"):
        out.append(f"\n{data['message']}")
        return "\n".join(out)

    if data.get("synthesis"):
        out.append(f"\n## Why this code is the way it is\n\n{data['synthesis']}")

    results = data.get("results") or []
    if results:
        out.append("\n## Discussions")
    for i, r in enumerate(results, 1):
        badge = (
            " **(exact match: references the PR that introduced these lines)**"
            if r["match_type"] == "exact"
            else ""
        )
        out.append(f"\n### [{i}] #{r['channel_name']} — {r['date']}{badge}\n")
        if r.get("why"):
            out.append(f"\n*{r['why']}*\n")
        out.append(f"\n{r['summary']}\n")
        if r.get("raw_text"):
            out.append(f"\n<details>\n\n```\n{r['raw_text'][:2000]}\n```\n\n</details>\n")
        out.append(f"\n{r['permalink']}\n")

    out.append(
        "\n---\nTreat these discussions as constraints: if they explain a value "
        "or a special case in this code, a change that ignores them is likely a "
        "regression."
    )
    return "\n".join(out)


def main() -> None:
    try:
        config.require_api_key()
    except config.MissingAPIKey as exc:
        raise SystemExit(f"\n{exc}\n")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
