"""Entity resolution: blame + hits + conflicts -> a provenance graph.

Deterministic, zero LLM calls, no network. Runs in single-digit milliseconds and is
instrumented as its own Sentry span (`resolve_graph`) specifically so that claim is
checkable in a trace rather than asserted (13).

Every edge added from structural evidence is `confidence="exact"` -- it was proven by
git or by an identity match, not inferred. Only the conflict/supersede edges the
synthesis step flagged are `confidence="llm-flagged"`, and the extension renders
those dashed.
"""

from __future__ import annotations

from .. import models
from ..integrations import github, sentry_issues, tickets


def resolve(
    blame: models.BlameInfo,
    hits: list[dict],
    conflicts: list[models.ConflictPair],
) -> models.Graph:
    nodes: dict[str, models.GraphNode] = {}
    edges: list[models.GraphEdge] = []

    def node(id_, type_, label, data=None):
        if id_ not in nodes:
            nodes[id_] = models.GraphNode(id=id_, type=type_, label=label, data=data or {})
        return id_

    code_id = node("code", "Code", "Selected code")

    # A PR can be reached twice -- once from blame, once from a thread that names it --
    # and its ticket/incident must be hung off it exactly once either way.
    enriched_prs: set[int] = set()

    def attach_pr_context(pr_id: str, pr: int) -> None:
        if pr in enriched_prs:
            return
        enriched_prs.add(pr)

        # Adapters return lists: a PR can close two tickets or touch two incidents,
        # and it can match none at all (18 rows 23-24).
        for t in tickets.lookup_by_pr(pr):
            ticket_id = node(
                f"ticket:{t['key']}", "Ticket", t["key"],
                {"title": t.get("title"), "status": t.get("status"),
                 "assignee": t.get("assignee")},
            )
            edges.append(models.GraphEdge(
                source=pr_id, target=ticket_id, type="TRACKED_BY", confidence="exact"
            ))

        for issue in sentry_issues.lookup_by_pr(pr):
            issue_id = node(
                f"sentry:{issue['id']}", "SentryIssue", issue["id"],
                {"title": issue.get("title"), "status": issue.get("status"),
                 "first_seen": issue.get("first_seen")},
            )
            edges.append(models.GraphEdge(
                source=pr_id, target=issue_id, type="RELATED_TO", confidence="exact"
            ))

    if blame.dominant_sha:
        commit_id = node(
            f"commit:{blame.dominant_sha}", "Commit", blame.dominant_sha,
            {"authors": blame.authors, "date": blame.commit_date},
        )
        edges.append(models.GraphEdge(
            source=code_id, target=commit_id, type="CREATED_BY", confidence="exact"
        ))

        for author in blame.authors:
            person_id = node(f"person:{author}", "Person", author)
            edges.append(models.GraphEdge(
                source=commit_id, target=person_id, type="AUTHORED_BY", confidence="exact"
            ))

        for pr in blame.pr_numbers:
            pr_id = node(f"pr:{pr}", "PullRequest", f"PR #{pr}")
            edges.append(models.GraphEdge(
                source=commit_id, target=pr_id, type="PART_OF", confidence="exact"
            ))

            gh = github.lookup_by_pr(pr)
            if gh:
                nodes[pr_id].data.update({
                    "title": gh.get("title"),
                    "author": gh.get("author"),
                    "merged_at": gh.get("merged_at"),
                })

            attach_pr_context(pr_id, pr)

    slack_ids: list[str] = []

    for h in hits:
        p = h["payload"]
        slack_id = node(
            f"slack:{p['channel_id']}/{p['thread_id']}", "SlackThread",
            f"#{p['channel_name']}",
            {
                "date": p.get("date_str"),
                "permalink": p.get("permalink"),
                "summary": p.get("summary", "")[:200],
            },
        )
        slack_ids.append(slack_id)

        # Whether this thread reached the graph through proof or through ranking.
        # An unanchored thread would otherwise render as a node with no edges at all.
        anchored = False

        # A thread that names a PR the code actually belongs to is an asserted
        # relationship, not a scored one (2.3).
        for pr in p.get("pr_refs", []):
            pr_id = f"pr:{pr}"
            if pr_id in nodes:
                edges.append(models.GraphEdge(
                    source=pr_id, target=slack_id, type="DISCUSSED_IN", confidence="exact"
                ))
                anchored = True
                continue

            # Blame never reached this PR, but the thread names it by number. That is
            # an identity match, so the PR (and its ticket and incident) belong on the
            # map -- hung off the thread, not off the code, because a mention is not
            # proof that this code is part of that PR. A PR the adapters have never
            # heard of still adds no node (18 row 23).
            gh = github.lookup_by_pr(pr)
            if gh:
                node(pr_id, "PullRequest", f"PR #{pr}", {
                    "title": gh.get("title"),
                    "author": gh.get("author"),
                    "merged_at": gh.get("merged_at"),
                })
                edges.append(models.GraphEdge(
                    source=slack_id, target=pr_id, type="REFERENCES", confidence="exact"
                ))
                attach_pr_context(pr_id, pr)
                anchored = True

        for key in p.get("ticket_refs", []):
            ticket_id = f"ticket:{key}"
            if ticket_id not in nodes:
                t = tickets.lookup_by_key(key)
                if t:
                    node(ticket_id, "Ticket", t["key"], {
                        "title": t.get("title"), "status": t.get("status"),
                        "assignee": t.get("assignee"),
                    })
            if ticket_id in nodes:
                edges.append(models.GraphEdge(
                    source=slack_id, target=ticket_id, type="REFERENCES", confidence="exact"
                ))
                anchored = True

        if not anchored:
            # Retrieved on similarity alone. Dashed, because nothing proved it.
            edges.append(models.GraphEdge(
                source=code_id, target=slack_id, type="DISCUSSED_IN",
                confidence="llm-flagged",
            ))

    # With no commit anchor, every thread can still have reached the graph through a
    # PR or ticket of its own -- which would leave the selection itself sitting off to
    # one side, attached to nothing. Tie it to the evidence it retrieved.
    if not any(e.source == code_id or e.target == code_id for e in edges):
        for slack_id in slack_ids:
            edges.append(models.GraphEdge(
                source=code_id, target=slack_id, type="DISCUSSED_IN",
                confidence="llm-flagged",
            ))

    citation_to_node = {
        i + 1: f"slack:{h['payload']['channel_id']}/{h['payload']['thread_id']}"
        for i, h in enumerate(hits)
    }
    for c in conflicts:
        a, b = citation_to_node.get(c.a), citation_to_node.get(c.b)
        if a and b and a in nodes and b in nodes:
            edge_type = "SUPERSEDES" if c.kind == "supersede" else "CONFLICTS_WITH"
            edges.append(models.GraphEdge(
                source=a, target=b, type=edge_type, confidence="llm-flagged"
            ))

    return models.Graph(nodes=list(nodes.values()), edges=edges)
