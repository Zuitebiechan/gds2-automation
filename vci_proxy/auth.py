"""
PSK authentication via HMAC-SHA256 (P1-2).

The pre-shared key (token) is never sent in plaintext. Instead, the client
sends an HMAC-SHA256 signature over its current timestamp. The server verifies
the signature and rejects timestamps with >5 minute drift (replay protection).
"""

import hashlib
import hmac
import struct
import time
import logging
from typing import Tuple

logger = logging.getLogger(__name__)

MAX_DRIFT_S = 300  # 5 minutes


def compute_signature(token: str, timestamp: int) -> bytes:
    """Compute HMAC-SHA256(token, timestamp) -> 32 bytes."""
    ts_bytes = struct.pack(">Q", timestamp)
    return hmac.new(token.encode("utf-8"), ts_bytes, hashlib.sha256).digest()


def verify_signature(
    token: str, timestamp: int, signature: bytes
) -> Tuple[bool, str]:
    """Verify an HMAC-SHA256 signature with replay protection.

    Returns (success, reason).
    """
    now = int(time.time())
    drift = abs(now - timestamp)
    if drift > MAX_DRIFT_S:
        return False, f"timestamp drift too large: {drift}s > {MAX_DRIFT_S}s"

    expected = compute_signature(token, timestamp)
    if not hmac.compare_digest(expected, signature):
        return False, "HMAC signature mismatch"

    return True, "ok"
