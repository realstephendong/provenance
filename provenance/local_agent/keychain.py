"""Where the two secrets on a developer's machine live: the store key and the Slack
refresh token.

Preference order is the OS credential store (Keychain on macOS, Credential Manager
on Windows, Secret Service on Linux) via `keyring`, falling back to a 0600 file
inside the profile directory. The fallback is real -- a headless Linux box often has
no Secret Service -- but it is weaker, so it says so once, out loud, rather than
degrading quietly. A person who believes their Slack token is in the Keychain when
it is in a file cannot make an informed decision about their own laptop.

Never: `.env`, workspace settings, anything inside a git repository, or a log line.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

SERVICE = "provenance-local"
_warned = False


def _keyring():
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailBackend
    except ImportError:
        return None
    try:
        if isinstance(keyring.get_keyring(), FailBackend):
            return None
    except Exception:
        return None
    return keyring


def backend_name() -> str:
    kr = _keyring()
    if kr is None:
        return "file"
    try:
        return type(kr.get_keyring()).__name__
    except Exception:          # pragma: no cover - backend probing is best effort
        return "keyring"


def _warn_file_fallback(path: Path) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    print(
        f"  ! no OS credential store available; secrets are in {path} with 0600 "
        "permissions.\n"
        "    Anyone who can read your home directory can read them. Install a "
        "keyring backend for stronger protection."
    )


def _file(profile_dir: Path, name: str) -> Path:
    return profile_dir / "secrets" / name


def account(profile_dir: Path, name: str) -> str:
    """The credential-store account name for one secret of one profile.

    Keyed on the profile's *absolute path*, not its directory name. An OS credential
    store is machine-global: two checkouts both using the profile called "default"
    would otherwise read and overwrite each other's Slack token and store key, and
    the symptom -- one workspace's credentials appearing under another's profile --
    is not one anybody would guess from.
    """
    try:
        resolved = profile_dir.resolve()
    except OSError:                    # pragma: no cover - unresolvable path
        resolved = profile_dir
    return f"{resolved}:{name}"


def get(profile_dir: Path, name: str) -> str:
    kr = _keyring()
    key = account(profile_dir, name)
    if kr is not None:
        try:
            return kr.get_password(SERVICE, key) or ""
        except Exception:
            pass
    path = _file(profile_dir, name)
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def put(profile_dir: Path, name: str, value: str) -> None:
    kr = _keyring()
    key = account(profile_dir, name)
    if kr is not None:
        try:
            kr.set_password(SERVICE, key, value)
            return
        except Exception:
            pass
    path = _file(profile_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with the right mode from the start: writing then chmod leaves a window
    # in which the file is world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as handle:
        handle.write(value)
    _warn_file_fallback(path)


def delete(profile_dir: Path, name: str) -> None:
    kr = _keyring()
    key = account(profile_dir, name)
    if kr is not None:
        try:
            kr.delete_password(SERVICE, key)
        except Exception:
            pass
    path = _file(profile_dir, name)
    try:
        path.unlink()
    except OSError:
        pass
