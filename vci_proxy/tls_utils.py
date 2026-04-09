"""Shared TLS hardening helpers for the reverse tunnel."""

from __future__ import annotations

import ssl


def harden_tls_context(context: ssl.SSLContext) -> ssl.SSLContext:
    """Apply consistent minimum TLS settings without weakening newer defaults."""
    tls_version = getattr(ssl, "TLSVersion", None)
    minimum_tls = getattr(tls_version, "TLSv1_2", None)
    if minimum_tls is None or not hasattr(context, "minimum_version"):
        return context

    current_minimum = getattr(context, "minimum_version", None)
    if current_minimum is None or current_minimum < minimum_tls:
        context.minimum_version = minimum_tls

    return context
