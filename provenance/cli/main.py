"""Terminal surface.

    provenance explain <file>:<start>-<end> [--repo <path>]
    provenance graph <commit-sha> [--repo <path>]

`explain` consumes the identical ContextResponse the VS Code panel and the MCP server
consume. It contains no retrieval logic of its own, and never will (18 row 32) --
it POSTs to the same service and renders what comes back.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

from .. import config
from ..models import ContextResponse

TARGET = re.compile(r"^(?P<file>.+?):(?P<start>\d+)-(?P<end>\d+)$")
REQUEST_TIMEOUT_SECONDS = 90

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def _style(text: str, code: str) -> str:
    return text if not sys.stdout.isatty() else f"{code}{text}{RESET}"


def _rule(title: str) -> str:
    return _style(f"\n{title}\n{'-' * len(title)}", BOLD)


# --- explain ------------------------------------------------------------------


def _read_range(repo_root: Path, file_path: str, start: int, end: int) -> str:
    path = repo_root / file_path
    if not path.is_file():
        sys.exit(f"no such file: {path}")
    lines = path.read_text(errors="replace").splitlines(keepends=True)
    if start > len(lines):
        sys.exit(f"{file_path} has {len(lines)} lines; {start} is past the end")
    return "".join(lines[start - 1:end])


def _render_graph(response: ContextResponse) -> str:
    """A terminal has no SVG, so the graph is an indented walk from the code node."""
    graph = response.graph
    if not graph.nodes:
        return "  (nothing resolved)"

    by_id = {n.id: n for n in graph.nodes}
    outgoing: dict[str, list] = {}
    for e in graph.edges:
        outgoing.setdefault(e.source, []).append(e)

    lines: list[str] = []
    seen: set[str] = set()

    def walk(node_id: str, depth: int) -> None:
        node = by_id.get(node_id)
        if node is None:
            return
        pad = "  " + "   " * depth
        title = node.data.get("title") or node.data.get("summary") or ""
        suffix = f"  {_style(title[:60], DIM)}" if title else ""
        lines.append(f"{pad}{node.type}: {node.label}{suffix}")
        if node_id in seen:
            return
        seen.add(node_id)
        for edge in outgoing.get(node_id, []):
            marker = "~" if edge.confidence == "llm-flagged" else "-"
            lines.append(f"{pad}   {marker}{edge.type}{marker}>")
            walk(edge.target, depth + 1)

    roots = [n.id for n in graph.nodes if n.id == "code"] or [graph.nodes[0].id]
    for root in roots:
        walk(root, 0)

    orphans = [n for n in graph.nodes if n.id not in seen]
    if orphans:
        lines.append("  (unlinked)")
        for n in orphans:
            lines.append(f"     {n.type}: {n.label}")
    return "\n".join(lines)


def render(response: ContextResponse, target: str) -> str:
    out: list[str] = []
    blame = response.blame

    out.append(_style(f"\n{target}", BOLD))
    if blame.dominant_sha:
        authors = ", ".join(blame.authors) or "unknown"
        head = f"  {blame.dominant_sha}  {blame.commit_date or ''}  {authors}"
        if blame.pr_numbers:
            head += "  " + ", ".join(f"#{p}" for p in blame.pr_numbers)
        out.append(head)
    elif blame.uncommitted:
        out.append("  (uncommitted lines -- no commit anchor)")
    else:
        out.append("  (no git history for this range)")

    if response.message:
        out.append(f"\n  {response.message}")
    if response.error:
        out.append(_style(f"  {response.error}", DIM))

    if response.synthesis:
        out.append(_rule("WHY"))
        out.append(f"  {response.synthesis}")

    if response.results:
        out.append(_rule("EVIDENCE"))
        for i, r in enumerate(response.results, start=1):
            tag = _style(f"[{r.match_type.upper()}]", BOLD)
            out.append(f"\n  [{i}] {tag} #{r.channel_name}  {r.date}  score={r.score:.4f}")
            out.append(f"      {r.summary}")
            if r.why:
                out.append(_style(f"      why: {r.why}", DIM))
            if r.participants:
                out.append(_style(f"      who: {', '.join(r.participants)}", DIM))
            out.append(_style(f"      {r.permalink}", DIM))

    if response.conflicts:
        out.append(_rule("CONFLICTS"))
        for c in response.conflicts:
            if c.kind == "supersede":
                out.append(f"  [{c.b}] superseded by [{c.a}] -- treat [{c.a}] as current")
            else:
                out.append(f"  [{c.a}] and [{c.b}] disagree -- the merged PR is ground truth")

    out.append(_rule("GRAPH"))
    out.append(_render_graph(response))

    if response.timing_ms:
        timings = "  ".join(f"{k}={v}ms" for k, v in response.timing_ms.items())
        out.append(_style(f"\n  {timings}", DIM))

    out.append(_style(
        "\n  options: --json for the raw response | "
        "`provenance graph <sha>` for a commit's chain\n", DIM
    ))
    return "\n".join(out)


def cmd_explain(args: argparse.Namespace) -> int:
    match = TARGET.match(args.target)
    if not match:
        sys.exit("target must look like path/to/file.py:20-40")
    file_path = match.group("file")
    start, end = int(match.group("start")), int(match.group("end"))
    if end < start:
        sys.exit("end line must be >= start line")

    repo_root = Path(args.repo).resolve()
    code = _read_range(repo_root, file_path, start, end)

    payload = {
        "code": code, "file_path": file_path, "repo_root": str(repo_root),
        "line_start": start, "line_end": end,
    }
    try:
        resp = httpx.post(
            f"{config.SERVICE_URL}/context", json=payload, timeout=REQUEST_TIMEOUT_SECONDS
        )
        resp.raise_for_status()
    except Exception as exc:
        sys.exit(f"could not reach the provenance service at {config.SERVICE_URL}: {exc}")

    if args.json:
        print(json.dumps(resp.json(), indent=2))
        return 0
    print(render(ContextResponse.model_validate(resp.json()), args.target))
    return 0


# --- graph --------------------------------------------------------------------


def cmd_graph(args: argparse.Namespace) -> int:
    """A commit's structural chain. Deterministic and local -- no retrieval, so no
    service and no LLM call is involved."""
    from ..models import BlameInfo
    from ..service import gitctx, graph as graph_mod

    repo_root = str(Path(args.repo).resolve())
    pr = gitctx.sha_to_pr(repo_root, args.sha)
    if pr is None:
        print(f"  no pull request resolves from {args.sha}")

    blame = BlameInfo(
        dominant_sha=args.sha[:7],
        all_shas=[args.sha[:7]],
        pr_number=pr,
        pr_numbers=[pr] if pr is not None else [],
    )
    response = ContextResponse(blame=blame, graph=graph_mod.resolve(blame, [], []))
    print(_rule(f"GRAPH {args.sha[:7]}"))
    print(_render_graph(response))
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="provenance", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    explain = sub.add_parser("explain", help="explain a line range")
    explain.add_argument("target", help="path/to/file.py:20-40")
    explain.add_argument("--repo", default=".", help="repository root (default: cwd)")
    explain.add_argument("--json", action="store_true", help="print the raw response")
    explain.set_defaults(func=cmd_explain)

    graph = sub.add_parser("graph", help="show a commit's evidence chain")
    graph.add_argument("sha")
    graph.add_argument("--repo", default=".", help="repository root (default: cwd)")
    graph.set_defaults(func=cmd_graph)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
