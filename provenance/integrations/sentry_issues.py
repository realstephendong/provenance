"""Error-tracker adapter. Fixtures while USE_MOCK_DATA is on, Sentry's API once it is
off and SENTRY_API_TOKEN, SENTRY_ORG and SENTRY_PROJECT are all set. With the flag off
and no Sentry configured, every lookup is empty -- code that caused no incident is the
common case, not an error.

`lookup_by_pr` returns a *list*: a PR can relate to more than one incident.

"Which incidents relate to PR #4821" is not a relation Sentry models directly. The
fixture stores `pr_number` on the issue because we control the fixture; the live path
goes the long way round, through the one identifier both systems already agree on:

    PR #4821  --GitHub-->  merge_commit_sha  --Sentry-->  firstRelease:<sha>

That holds only because the analysed repository names each Sentry release after the
merge commit it shipped -- `getsentry/action-release` defaults to `github.sha`, which
for a merge commit is the same string GitHub reports as `merge_commit_sha`. See
`.github/workflows/sentry-release.yml` in the repo being analysed. Given that, the
join is exact in the same sense the git->PR join is: nothing is scored or guessed.

Version your releases some other way -- semver, a build number, a container tag --
and this degrades to finding nothing rather than to finding something wrong. The one
function to change is `_release_for_pr`; everything above it is release-agnostic.

`firstRelease` and not `release` on purpose. `release:` returns every issue *seen* in
a release, which for a long-lived error means every PR since it appeared inherits the
blame. `firstRelease:` returns the issues that first appeared in it, which is the
question the graph is actually asking.

`lookup_by_id` has no such problem: a short id is an exact key on both backends.
"""

from __future__ import annotations

import json

from .. import config
from . import _live, github

_CACHE: list[dict] | None = None


def _load() -> list[dict]:
    global _CACHE
    if _CACHE is None:
        path = config.SEED_DIR / "mock_integrations" / "sentry_issues.json"
        try:
            _CACHE = json.loads(path.read_text()) if path.exists() else []
        except (json.JSONDecodeError, OSError):
            _CACHE = []
    return _CACHE


# --- live backend -------------------------------------------------------------

def live_enabled() -> bool:
    return bool(config.SENTRY_API_TOKEN and config.SENTRY_ORG and config.SENTRY_PROJECT)


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.SENTRY_API_TOKEN}"}


def _normalize(issue: dict, pr_number: int | None = None) -> dict:
    issue_id = issue.get("shortId") or issue.get("id")
    permalink = issue.get("permalink")
    if not permalink and config.SENTRY_ORG and issue_id:
        permalink = f"{config.SENTRY_WEB_URL}/organizations/{config.SENTRY_ORG}/issues/{issue_id}/"
    return {
        "id": issue_id,
        "title": issue.get("title"),
        "first_seen": issue.get("firstSeen"),
        "status": issue.get("status"),
        "pr_number": pr_number,
        "url": permalink,
    }


def _release_for_pr(pr_number: int) -> str | None:
    """The Sentry release version a PR shipped as: its merge commit SHA.

    Costs no extra request in practice -- `resolve_graph` has already asked the
    GitHub adapter about this PR to put a title on the node, and `_live.get_json`
    caches by URL. An unmerged PR has no merge commit and so no release, which is
    correct: code that never landed caused no incident.
    """
    pr = github.lookup_by_pr(pr_number)
    return (pr or {}).get("merge_commit_sha") or None


def _live_by_pr(pr_number: int) -> list[dict]:
    release = _release_for_pr(pr_number)
    if not release:
        return []
    data = _live.get_json(
        f"{config.SENTRY_API}/projects/{config.SENTRY_ORG}/{config.SENTRY_PROJECT}/issues/",
        headers=_headers(),
        # An empty statsPeriod means "no date filter". The default is 14d, which
        # would hide exactly the incidents this tool exists to surface -- the whole
        # point of asking is that the PR is older than anyone's memory of it.
        # The explicit query also overrides Sentry's default `is:unresolved`; a
        # resolved incident is still the reason the code looks the way it does.
        params={"query": f"firstRelease:{release}", "statsPeriod": ""},
    )
    if not isinstance(data, list):
        return []
    return [_normalize(i, pr_number) for i in data if isinstance(i, dict)]


def _live_by_id(issue_id: str) -> dict | None:
    data = _live.get_json(
        f"{config.SENTRY_API}/organizations/{config.SENTRY_ORG}/shortids/{issue_id}/",
        headers=_headers(),
    )
    if isinstance(data, dict):
        group = data.get("group")
        if isinstance(group, dict):
            # The shortids endpoint nests the issue and reports the canonical short id
            # alongside it; prefer that over the one we were asked about.
            return _normalize({**group, "shortId": data.get("shortId") or issue_id})
    return None


# --- public interface ---------------------------------------------------------

def lookup_by_pr(pr_number: int) -> list[dict]:
    if not config.USE_MOCK_DATA:
        return _live_by_pr(pr_number) if live_enabled() else []
    return [i for i in _load() if i.get("pr_number") == pr_number]


def lookup_by_id(issue_id: str) -> dict | None:
    if not config.USE_MOCK_DATA:
        return _live_by_id(issue_id) if live_enabled() else None
    for i in _load():
        if i.get("id") == issue_id:
            return i
    return None
