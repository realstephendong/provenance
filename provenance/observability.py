"""Sentry is optional. If SENTRY_DSN is unset, every function here is a no-op --
nothing in the pipeline may depend on Sentry being configured to function correctly."""

from __future__ import annotations

from contextlib import contextmanager

from . import config

_enabled = False


def init() -> None:
    global _enabled
    if not config.SENTRY_DSN:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.openai import OpenAIIntegration

        sentry_sdk.init(
            dsn=config.SENTRY_DSN,
            integrations=[FastApiIntegration(), OpenAIIntegration()],
            traces_sample_rate=1.0,
            send_default_pii=False,
        )
        _enabled = True
    except Exception as exc:  # pragma: no cover - Sentry must never break boot
        print(f"  ! sentry init failed, continuing without it: {exc}")


@contextmanager
def span(op: str, description: str = ""):
    if not _enabled:
        yield
        return
    import sentry_sdk

    with sentry_sdk.start_span(op=op, description=description or op):
        yield


def log_info(message: str, **extra) -> None:
    if not _enabled:
        return
    import sentry_sdk

    with sentry_sdk.push_scope() as scope:
        for key, value in extra.items():
            scope.set_extra(key, value)
        sentry_sdk.capture_message(message, level="info")
