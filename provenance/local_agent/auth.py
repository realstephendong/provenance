"""Two independent authentications, for two different threats.

**The launch secret** answers "is this request really from the editor that started
me?". The connector listens on a loopback port, and on a shared machine every local
process can reach a loopback port. So the extension generates a random secret,
passes it to the connector at spawn time, and sends it on every call. A request
without it is refused before it reaches any stored data.

**Slack user OAuth** answers "which Slack account's private conversations may this
connector read?". Authorization Code with PKCE, run from the device: the browser
sends the person to Slack, Slack redirects to the connector's own loopback callback,
and the connector exchanges the code itself. The central service is never in that
path and never sees the resulting token -- which is the whole point, because a
central service holding user tokens for private channels is central custody of
private content by another name.

One honest limitation, stated rather than hidden: Slack's token exchange accepts a
`code_verifier`, but a Slack app configured as a *confidential* client will still
require its client secret. Three modes are supported, in descending order of safety:

  1. public client + PKCE -- nothing on the device can impersonate the app;
  2. confidential client -- needs SLACK_CLIENT_SECRET present locally, which is a
     real weakening and is reported in `/v1/status` so nobody has to guess;
  3. paste an existing user token (`xoxp-...`) -- no browser flow at all, for
     workspaces where app installation is restricted.

The token never touches `.env`, workspace settings, a log line, or the connector's
own database. It lives in the OS credential store; see `keychain.py`.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from .. import config
from . import keychain

TOKEN_NAME = "slack_user_token"
REFRESH_NAME = "slack_refresh_token"
SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET", "").strip()


class NotSignedIn(RuntimeError):
    """No Slack credential for this profile."""


class OAuthFailed(RuntimeError):
    """The browser flow did not produce a usable token."""


# --- launch secret ----------------------------------------------------------------


class LaunchSecret:
    """A per-launch shared secret between the editor and this process.

    Taken from the environment when the extension supplied one (the normal path),
    generated otherwise. Compared in constant time -- a loopback attacker gets
    unlimited attempts, so a short-circuiting `==` would leak the secret a byte at a
    time.
    """

    ENV = "PROVENANCE_LOCAL_SECRET"
    HEADER = "x-provenance-local-secret"

    def __init__(self, value: str = ""):
        self.value = value or os.environ.get(self.ENV, "").strip() or secrets.token_urlsafe(32)
        self.supplied = bool(value or os.environ.get(self.ENV, "").strip())

    def matches(self, presented: str | None) -> bool:
        return bool(presented) and secrets.compare_digest(self.value, presented or "")


# --- PKCE flow ---------------------------------------------------------------------


@dataclass
class PendingAuth:
    state: str
    verifier: str
    redirect_uri: str
    created_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > config.LOCAL_OAUTH_STATE_TTL_SECONDS


class SlackAuth:
    def __init__(self, store, *, client_id: str = "", http: httpx.Client | None = None):
        self.store = store
        self.client_id = client_id or config.SLACK_CLIENT_ID
        self._pending: dict[str, PendingAuth] = {}
        self._lock = threading.Lock()
        self._http = http

    # --- credential --------------------------------------------------------------

    def token(self) -> str:
        """The Slack user token for this profile.

        `SLACK_USER_TOKEN` is honoured as an import path so a person who already has
        a working token from the single-deployment setup does not have to redo the
        browser flow -- but it is copied into the credential store on first use and
        read from there afterwards, so it does not have to stay in an env file.
        """
        stored = keychain.get(self.store.root, TOKEN_NAME)
        if stored:
            return stored
        if config.SLACK_USER_TOKEN:
            keychain.put(self.store.root, TOKEN_NAME, config.SLACK_USER_TOKEN)
            return config.SLACK_USER_TOKEN
        return ""

    def require_token(self) -> str:
        token = self.token()
        if not token:
            raise NotSignedIn(
                "this profile is not signed in to Slack. Run 'Provenance: Connect "
                "Slack (private)' in the editor."
            )
        return token

    def save_token(self, token: str, refresh_token: str = "") -> None:
        keychain.put(self.store.root, TOKEN_NAME, token)
        if refresh_token:
            keychain.put(self.store.root, REFRESH_NAME, refresh_token)

    def revoke(self, *, call_slack: bool = True) -> dict:
        """Forget the credential, and tell Slack to invalidate it.

        Order matters: revoke at Slack first, then delete locally. Deleting first
        would leave a live token we can no longer name if the network call fails.
        """
        token = self.token()
        revoked_remotely = False
        if token and call_slack:
            try:
                response = self._client().post(
                    f"{config.SLACK_API}/auth.revoke",
                    headers={"Authorization": f"Bearer {token}"},
                )
                revoked_remotely = bool(response.json().get("ok"))
            except (httpx.HTTPError, ValueError):
                revoked_remotely = False
        keychain.delete(self.store.root, TOKEN_NAME)
        keychain.delete(self.store.root, REFRESH_NAME)
        return {"signed_in": False, "revoked_at_slack": revoked_remotely}

    # --- browser flow -------------------------------------------------------------

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=config.SLACK_TIMEOUT_SECONDS)
        return self._http

    @staticmethod
    def _challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def start(self, redirect_uri: str) -> dict:
        if not self.client_id:
            raise OAuthFailed(
                "SLACK_CLIENT_ID is not set, so the browser sign-in cannot start. "
                "Set it, or paste an existing user token instead."
            )
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(24)
        with self._lock:
            self._expire()
            self._pending[state] = PendingAuth(state, verifier, redirect_uri)
        query = urlencode({
            "client_id": self.client_id,
            "user_scope": ",".join(config.SLACK_USER_SCOPES),
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": self._challenge(verifier),
            "code_challenge_method": "S256",
        })
        return {"authorize_url": f"{config.SLACK_OAUTH_AUTHORIZE_URL}?{query}",
                "state": state}

    def _expire(self) -> None:
        for state, pending in list(self._pending.items()):
            if pending.expired:
                del self._pending[state]

    def complete(self, state: str, code: str) -> dict:
        """Exchange the authorization code. Single-use: the state is consumed here.

        An unknown or expired state is refused outright. That check is what stops a
        web page the person happens to have open from driving this endpoint -- it
        can reach loopback, but it cannot know a state it did not start.
        """
        with self._lock:
            self._expire()
            pending = self._pending.pop(state, None)
        if pending is None:
            raise OAuthFailed("that sign-in link has expired or was already used")
        if not code:
            raise OAuthFailed("Slack did not return an authorization code")

        form = {
            "client_id": self.client_id,
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "code_verifier": pending.verifier,
        }
        if SLACK_CLIENT_SECRET:
            form["client_secret"] = SLACK_CLIENT_SECRET
        try:
            body = self._client().post(f"{config.SLACK_API}/oauth.v2.access",
                                       data=form).json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthFailed(f"could not reach Slack to exchange the code: {exc}") from exc
        if not body.get("ok"):
            error = body.get("error", "unknown_error")
            if error in {"invalid_client_id", "bad_client_secret"} and not SLACK_CLIENT_SECRET:
                raise OAuthFailed(
                    "Slack rejected the exchange without a client secret. This app is "
                    "configured as a confidential client: set SLACK_CLIENT_SECRET "
                    "locally, or reconfigure the Slack app as a public client so PKCE "
                    "alone is enough."
                )
            raise OAuthFailed(f"Slack refused the sign-in ({error})")

        authed = body.get("authed_user") or {}
        token = authed.get("access_token") or ""
        if not token:
            raise OAuthFailed(
                "Slack returned a bot token but no user token. The app must request "
                "user scopes, not bot scopes, for private indexing."
            )
        self.save_token(token, authed.get("refresh_token", ""))
        team = body.get("team") or {}
        return {
            "signed_in": True,
            "user_id": authed.get("id", ""),
            "team_id": team.get("id", ""),
            "team_name": team.get("name", ""),
            "scopes": (authed.get("scope") or "").split(","),
        }

    def import_token(self, token: str) -> dict:
        """Accept a pasted `xoxp-` token, verifying it before storing it.

        Storing an unverified token means the first real failure arrives much later,
        somewhere unrelated, as 'Slack wouldn't let me read that'.
        """
        token = (token or "").strip()
        if not token.startswith("xoxp-"):
            raise OAuthFailed(
                "that does not look like a Slack *user* token. It starts with 'xoxp-'; "
                "'xoxb-' is a bot token and cannot read your private conversations."
            )
        try:
            body = self._client().post(
                f"{config.SLACK_API}/auth.test",
                headers={"Authorization": f"Bearer {token}"},
            ).json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthFailed(f"could not reach Slack to verify the token: {exc}") from exc
        if not body.get("ok"):
            raise OAuthFailed(f"Slack rejected that token ({body.get('error', 'unknown')})")
        self.save_token(token)
        return {"signed_in": True, "user_id": body.get("user_id", ""),
                "team_id": body.get("team_id", ""), "team_name": body.get("team", "")}

    def identity(self) -> dict:
        """Who this profile is signed in as, without exposing the token."""
        token = self.token()
        if not token:
            return {"signed_in": False}
        try:
            body = self._client().post(
                f"{config.SLACK_API}/auth.test",
                headers={"Authorization": f"Bearer {token}"},
            ).json()
        except (httpx.HTTPError, ValueError):
            # Offline is not signed out: the token is still there and will work when
            # the network comes back.
            return {"signed_in": True, "reachable": False}
        if not body.get("ok"):
            return {"signed_in": False, "error": body.get("error", "")}
        return {"signed_in": True, "reachable": True, "user_id": body.get("user_id", ""),
                "user": body.get("user", ""), "team_id": body.get("team_id", ""),
                "team_name": body.get("team", ""),
                "client_mode": "confidential" if SLACK_CLIENT_SECRET else "public+pkce"}
