"""Run the private connector.

    provenance-local serve [--profile default] [--port 0]
    provenance-local status
    provenance-local purge [--yes]

Normally the editor starts this and supervises it; the commands exist because a
person must be able to see and delete their own private index without the editor's
cooperation. A deletion feature you can only reach through the UI that created the
data is not really a deletion feature.

The port is ephemeral by default and reported on stdout as one JSON line before the
first request is served:

    {"provenance_local": {"port": 51234, "pid": 8412, "profile": "default"}}

The extension reads that line rather than guessing a port, which also means two
editor windows can run two connectors without colliding. A socket is bound here
rather than inside uvicorn so the port is known *before* anything is served -- the
OAuth redirect URI has to contain it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import socket
import stat
import sys
from pathlib import Path

from .. import config
from .api import AgentState, create_app
from .auth import LaunchSecret, SlackAuth
from .ingest import LocalIngest
from .store import CorruptStore, LocalStore, StoreLocked


def _bind(port: int) -> socket.socket:
    """Bind loopback, and refuse anything else.

    `127.0.0.1` is passed explicitly and is not configurable. A private index served
    on 0.0.0.0 is reachable from the coffee-shop wifi, and the only thing standing
    between it and a stranger would be a secret printed to stdout.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((config.LOCAL_AGENT_HOST, port))
    sock.listen(64)
    return sock


def _handshake(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)


def build_state(profile: str, secret: str = "") -> AgentState:
    store = LocalStore(profile)
    store.open()
    auth = SlackAuth(store)
    return AgentState(store=store, auth=auth, ingest=LocalIngest(store, auth),
                      secret=LaunchSecret(secret))


async def serve(args) -> None:
    import uvicorn

    state = build_state(args.profile, args.secret)
    sock = _bind(args.port)
    state.port = sock.getsockname()[1]

    handshake = {"provenance_local": {
        "port": state.port, "pid": os.getpid(), "profile": args.profile,
        "secret": "" if state.secret.supplied else state.secret.value,
    }}
    # The secret is echoed only when this process generated it (a person running the
    # connector by hand). When the editor supplied one, it is never printed.
    print(json.dumps(handshake), flush=True)
    if args.handshake_file:
        _handshake(Path(args.handshake_file), handshake["provenance_local"])

    server = uvicorn.Server(uvicorn.Config(
        create_app(state), log_level="warning", access_log=False,
    ))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, server.handle_exit, sig, None)
    try:
        await server.serve(sockets=[sock])
    finally:
        state.ingest.close()
        state.store.close()
        if args.handshake_file:
            with contextlib.suppress(OSError):
                Path(args.handshake_file).unlink()


def cmd_status(args) -> None:
    store = LocalStore(args.profile)
    if not store.path.exists():
        # A read-only command must not bring a profile (and an encryption key) into
        # existence as a side effect of being asked whether one exists.
        print(f"no private index for profile {args.profile!r} on this machine")
        print(f"  it would live at {store.path}")
        return
    try:
        store.open()
    except (StoreLocked, CorruptStore) as exc:
        sys.exit(str(exc))
    auth = SlackAuth(store)
    ingest = LocalIngest(store, auth)
    stats = store.stats()
    identity = auth.identity()
    print(f"profile      {stats['profile_id']}")
    print(f"store        {stats['path']}  ({stats['bytes']} bytes)")
    print(f"key          {stats['key_backend']}")
    print(f"slack        " + (
        f"{identity.get('user', '?')} @ {identity.get('team_name', '?')}"
        if identity.get("signed_in") else "not signed in"
    ))
    print(f"consent      {'granted' if ingest.consent() else 'not granted'}")
    print(f"documents    {stats['documents']} in {stats['channels']} channels")
    for channel in store.channels():
        print(f"  #{channel['channel_name']}  {channel['documents']} conversations")


def cmd_purge(args) -> None:
    store = LocalStore(args.profile)
    if not args.yes:
        print(f"This permanently deletes the private index at {store.path},")
        print("its encryption key and the stored Slack token. Nothing is recoverable.")
        if input("Type 'delete' to continue: ").strip() != "delete":
            sys.exit("cancelled")
    try:
        store.open()
    except (StoreLocked, CorruptStore):
        # An unreadable store still has to be deletable -- that is the case where a
        # person most wants it gone.
        pass
    result = store.purge()
    print(f"deleted {result['deleted_documents']} conversations from "
          f"{result['deleted_channels']} channels, and the key")


def main() -> None:
    parser = argparse.ArgumentParser(prog="provenance-local", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    serve_cmd = sub.add_parser("serve", help="run the connector (the editor does this)")
    serve_cmd.add_argument("--profile", default=config.LOCAL_DEFAULT_PROFILE)
    serve_cmd.add_argument("--port", type=int, default=config.LOCAL_AGENT_PORT)
    serve_cmd.add_argument("--secret", default="",
                           help="launch secret; normally passed as "
                                f"${LaunchSecret.ENV} instead so it is not in the "
                                "process list")
    serve_cmd.add_argument("--handshake-file", default="")
    serve_cmd.set_defaults(func=lambda a: asyncio.run(serve(a)))

    status_cmd = sub.add_parser("status", help="what is indexed on this machine")
    status_cmd.add_argument("--profile", default=config.LOCAL_DEFAULT_PROFILE)
    status_cmd.set_defaults(func=cmd_status)

    purge_cmd = sub.add_parser("purge", help="delete the private index and its key")
    purge_cmd.add_argument("--profile", default=config.LOCAL_DEFAULT_PROFILE)
    purge_cmd.add_argument("--yes", action="store_true")
    purge_cmd.set_defaults(func=cmd_purge)

    # `provenance-local` with no subcommand means "serve": that is how the editor
    # spawns it, and requiring the word would make the spawn line one typo away from
    # a usage error nobody sees.
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["serve", *argv]
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
