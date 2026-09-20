"""The connector's HTTP surface: loopback only, launch-secret authenticated.

Every route requires the per-launch secret the editor passed at spawn time, with one
necessary exception -- the OAuth callback, which arrives from a browser that cannot
know the secret. That route is protected instead by the single-use `state` the flow
itself created, which a page the person happens to have open cannot guess.

`/v1/health` is also readable without the secret and answers with nothing but a
liveness flag, so the editor can tell "connector running" from "connector wedged"
before it has a session.

Binding is asserted, not configured: this refuses to serve on anything but
127.0.0.1. A private index reachable from the network is not a private index, and
making that a setting invites exactly one bad afternoon.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import config
from ..models import BlameInfo, ContextRequest, ContextResponse
from ..service import gitctx, graph as graph_mod, query_build
from . import retrieve as local_retrieve
from .auth import LaunchSecret, NotSignedIn, OAuthFailed, SlackAuth
from .ingest import CONSENT_TEXT, ConsentRequired, LocalIngest, NotIndexable
from .store import CorruptStore, LocalStore, StoreLocked


@dataclass
class AgentState:
    store: LocalStore
    auth: SlackAuth
    ingest: LocalIngest
    secret: LaunchSecret
    port: int = 0

    @property
    def redirect_uri(self) -> str:
        return f"http://{config.LOCAL_AGENT_HOST}:{self.port}/v1/auth/slack/callback"


class TokenBody(BaseModel):
    token: str


class ConsentBody(BaseModel):
    grant: bool = True


_CALLBACK_PAGE = """<!doctype html><meta charset="utf-8">
<title>Provenance</title>
<style>body{{font:15px/1.6 system-ui,sans-serif;margin:12vh auto;max-width:34rem;
padding:0 1.5rem;color:#1d1d1f}}h1{{font-size:1.3rem}}.m{{color:#6e6e73}}
code{{background:#f2f2f4;padding:.1rem .35rem;border-radius:4px}}</style>
<h1>{title}</h1><p>{body}</p><p class="m">You can close this tab and return to your editor.</p>
"""


def create_app(state: AgentState) -> FastAPI:
    app = FastAPI(title="Provenance local connector", version="1.0.0")

    def guard(request: Request,
              x_provenance_local_secret: str | None = Header(default=None)) -> AgentState:
        if not state.secret.matches(x_provenance_local_secret):
            raise HTTPException(status_code=401, detail="launch secret required")
        return state

    # --- liveness and status ------------------------------------------------

    @app.get("/v1/health")
    def health() -> dict:
        return {"ok": True, "service": "provenance-local",
                "profile": state.store.profile_id}

    @app.get("/v1/status")
    def status(agent: AgentState = Depends(guard)) -> dict:
        try:
            stats = agent.store.stats()
        except (StoreLocked, CorruptStore) as exc:
            return {"ok": False, "error": str(exc), "scope": config.SCOPE_PRIVATE}
        consent = agent.ingest.consent()
        return {
            "ok": True,
            "scope": config.SCOPE_PRIVATE,
            "display_scope": config.display_scope(config.SCOPE_PRIVATE),
            "slack": agent.auth.identity(),
            "consent": {"granted": consent is not None,
                        "version": (consent or {}).get("version", ""),
                        "text": CONSENT_TEXT},
            "store": stats,
            "channels": agent.store.channels(),
            "embedder_id": config.EMBEDDER_ID,
        }

    # --- the private half of Backfill -------------------------------------------

    @app.post("/v1/backfill")
    async def backfill(agent: AgentState = Depends(guard)) -> dict:
        """Index this account's private channels, incrementally, on this machine.

        The panel calls this and `/ingest/sync` on the shared service together: one
        press, two planes, neither able to write to the other's store.
        """
        log: list[str] = []
        try:
            result = await agent.ingest.backfill(say=log.append)
        except ConsentRequired as exc:
            raise HTTPException(status_code=428, detail=str(exc)) from None
        except NotSignedIn as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from None
        except NotIndexable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return {"ok": True, **result, "scope": config.SCOPE_PRIVATE, "log": log}

    # --- retrieval ---------------------------------------------------------------

    @app.post("/v1/context", response_model=ContextResponse)
    async def context(req: ContextRequest,
                      agent: AgentState = Depends(guard)) -> ContextResponse:
        """Private evidence for a selection.

        Builds its own queries rather than reusing the service's. That is one extra
        code-to-prose call per explain, run in parallel with the shared one, and it
        buys retrieval parity: a private conversation is matched by exactly the query
        a shared one is matched by, so the merged list is ranked on one basis.
        """
        try:
            agent.store.open()
        except (StoreLocked, CorruptStore) as exc:
            return ContextResponse(message="The private index could not be opened.",
                                   error=str(exc))
        if not agent.auth.token():
            return ContextResponse(message="Not signed in to Slack for private results.")

        blame = (
            BlameInfo.model_validate(req.precomputed_blame)
            if req.precomputed_blame is not None
            else await asyncio.to_thread(
                gitctx.blame, req.repo_root, req.file_path,
                req.line_start, req.line_end, True,
            )
        )
        queries = await query_build.build_queries(req.code, req.file_path, req.language)
        found = await local_retrieve.retrieve(agent.store, queries, blame, req.file_path)
        hits = found["hits"]
        if not hits:
            return ContextResponse(blame=blame, message="No private discussions found.")

        graph = graph_mod.resolve(blame, hits, [], scope=config.SCOPE_PRIVATE)
        return ContextResponse(
            results=local_retrieve.to_results(hits), blame=blame, graph=graph,
        )

    # --- Slack sign-in -------------------------------------------------------------------

    @app.post("/v1/auth/slack/start")
    def auth_start(agent: AgentState = Depends(guard)) -> dict:
        try:
            return agent.auth.start(agent.redirect_uri)
        except OAuthFailed as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/auth/slack/callback", response_class=HTMLResponse)
    def auth_callback(request: Request) -> HTMLResponse:
        """Where Slack sends the browser back to.

        Reached without the launch secret -- a browser redirect cannot carry one --
        so the single-use `state` minted by `/v1/auth/slack/start` is the credential
        here. Parameters are read off the request rather than declared, because the
        query parameter is named `state` and the connector's own state object is
        already in scope under that name.
        """
        params = request.query_params
        error, supplied_state, code = (params.get("error", ""),
                                       params.get("state", ""),
                                       params.get("code", ""))
        if error:
            return HTMLResponse(_CALLBACK_PAGE.format(
                title="Slack sign-in cancelled",
                body=f"Slack reported <code>{error}</code>. Nothing was changed."),
                status_code=400)
        try:
            result = state.auth.complete(supplied_state, code)
        except OAuthFailed as exc:
            return HTMLResponse(_CALLBACK_PAGE.format(
                title="Sign-in failed", body=str(exc)), status_code=400)
        return HTMLResponse(_CALLBACK_PAGE.format(
            title="Connected to Slack",
            body=f"Provenance can now index the private channels you can read in "
                 f"<strong>{result.get('team_name') or 'your workspace'}</strong>, "
                 "on this machine only."))

    @app.post("/v1/auth/slack/token")
    def auth_token(body: TokenBody, agent: AgentState = Depends(guard)) -> dict:
        try:
            return agent.auth.import_token(body.token)
        except OAuthFailed as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/auth/slack/revoke")
    def auth_revoke(agent: AgentState = Depends(guard)) -> dict:
        agent.ingest.close()
        return agent.auth.revoke()

    # --- consent and deletion ---------------------------------------------------------------

    @app.post("/v1/consent")
    def consent(body: ConsentBody, agent: AgentState = Depends(guard)) -> dict:
        if body.grant:
            return {"granted": True, "record": agent.ingest.grant_consent()}
        agent.ingest.revoke_consent()
        return {"granted": False}

    @app.get("/v1/channels")
    def channels(agent: AgentState = Depends(guard)) -> dict:
        return {"channels": agent.store.channels()}

    @app.delete("/v1/channels/{channel_id}")
    def forget_channel(channel_id: str, agent: AgentState = Depends(guard)) -> dict:
        return {"deleted": agent.store.delete_channel(channel_id)}

    @app.delete("/v1/data")
    def purge(agent: AgentState = Depends(guard)) -> dict:
        """Delete the whole private index: conversations, checkpoints, consent, key
        and Slack token.

        Consent is revoked before the store is destroyed, so the record of having
        agreed does not outlive the data it was about.
        """
        agent.ingest.revoke_consent()
        agent.ingest.close()
        return agent.store.purge()

    return app
