"""Entity resolution: blame + hits + conflicts -> a provenance graph.

Deterministic and zero LLM calls. With the default fixture-backed adapters it also
makes no network call and runs in single-digit milliseconds; point any adapter at a
live API (see `integrations/_live.py`) and that second half stops holding, which is
exactly why this is instrumented as its own Sentry span (`resolve_graph`) -- the claim
is checkable in a trace rather than asserted (13).

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

    def attach_pr(pr: int, commit_id: str) -> str:
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
        return pr_id

    # One node per commit that owns a line of the range, not just the dominant one.
    # A range edited twice has two authors, two dates and two PRs; collapsing it to a
    # single node made the graph assert one origin it could not actually support, and
    # hung every PR off whichever commit happened to win the line count.
    commits = list(blame.commits)
    if not commits and blame.dominant_sha:
        # A BlameInfo built before `commits` existed (or by hand, as the eval suite
        # does) must still resolve to exactly the graph it used to produce.
        commits = [models.CommitInfo(
            sha=blame.dominant_sha, date=blame.commit_date, ts=blame.commit_ts,
            pr_number=blame.pr_number, dominant=True,
        )]

    dominant_commit_id: str | None = None
    for c in commits:
        commit_id = node(
            f"commit:{c.sha}", "Commit", c.sha,
            {
                "authors": [c.author] if c.author else [],
                "date": c.date,
                # `lines` is what makes "dominant" auditable rather than a bare flag.
                "lines": c.lines,
                "dominant": c.dominant,
            },
        )
        if c.dominant or dominant_commit_id is None:
            dominant_commit_id = commit_id
        edges.append(models.GraphEdge(
            source=code_id, target=commit_id, type="CREATED_BY", confidence="exact"
        ))

        if c.author:
            person_id = node(f"person:{c.author}", "Person", c.author)
            edges.append(models.GraphEdge(
                source=commit_id, target=person_id, type="AUTHORED_BY", confidence="exact"
            ))

        if c.pr_number is not None:
            attach_pr(c.pr_number, commit_id)

    if dominant_commit_id:
        # Anything the flat fields carry but the per-commit detail did not account for
        # -- a legacy BlameInfo, or an author git reported without a resolvable commit
        # -- still belongs on the map. Hang it off the dominant commit.
        for author in blame.authors:
            person_id = f"person:{author}"
            if person_id not in nodes:
                node(person_id, "Person", author)
                edges.append(models.GraphEdge(
                    source=dominant_commit_id, target=person_id,
                    type="AUTHORED_BY", confidence="exact",
                ))
        for pr in blame.pr_numbers:
            if f"pr:{pr}" not in nodes:
                attach_pr(pr, dominant_commit_id)

    slack_ids: list[str] = []

    for citation, h in enumerate(hits, start=1):
        p = h["payload"]
        slack_id = node(
            f"slack:{p['channel_id']}/{p['thread_id']}", "SlackThread",
            f"#{p['channel_name']}",
            {
                # `citation` is the [N] the synthesis and the evidence list use for this
                # thread. Carrying it on the node is what lets a click on the graph open
                # the matching evidence card instead of only a detail drawer.
                "citation": citation,
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

        # A thread that names a commit sha is naming *this* code, provided blame
        # actually resolved that commit. ingest has always extracted and indexed
        # `commit_shas` (extract.py's SHA regex, load.py's mapping); until now
        # nothing on the read side ever looked at them.
        for sha in p.get("commit_shas", []):
            commit_id = f"commit:{sha}"
            if commit_id in nodes:
                edges.append(models.GraphEdge(
                    source=slack_id, target=commit_id, type="REFERENCES", confidence="exact"
                ))
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
                continue

            # Same `ABC-123` shape, different tracker: an incident id the error
            # tracker knows and the ticket tracker has never heard of. Without this,
            # an incident only ever reached the graph through a PR -- so a thread that
            # discussed the incident directly, on code with no resolvable PR, lost it.
            issue_id = f"sentry:{key}"
            if issue_id not in nodes:
                issue = sentry_issues.lookup_by_id(key)
                if issue:
                    node(issue_id, "SentryIssue", issue["id"], {
                        "title": issue.get("title"), "status": issue.get("status"),
                        "first_seen": issue.get("first_seen"),
                    })
            if issue_id in nodes:
                edges.append(models.GraphEdge(
                    source=slack_id, target=issue_id, type="REFERENCES", confidence="exact"
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
