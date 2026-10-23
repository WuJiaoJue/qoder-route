"""Outbound TLS policy for the Qoder hosts (``*.qoder.sh``).

Why this module exists
----------------------
This backend runs on the uv-managed CPython in ``backend/.venv``, which links
OpenSSL 3.5. OpenSSL 3.5 enables the post-quantum hybrid group
``X25519MLKEM768`` by default, so the ClientHello carries an extra ~1 KB of
key-share material:

    default groups (PQ on)  ->  ClientHello 1520 bytes  (2 TCP segments)
    pinned X25519           ->  ClientHello  517 bytes  (1 TCP segment)

The network path between this host and ``*.qoder.sh`` (api1/api2/api3 and
openapi) silently drops that two-segment ClientHello, so every handshake dies
with::

    httpx.ConnectTimeout: _ssl.c:999: The handshake operation timed out

Curl and the system Python 3.10 (OpenSSL 3.0.2, no PQ group) are unaffected,
which is why ``curl https://api3.qoder.sh/`` works while the app cannot reach
upstream at all. Pinning the classical X25519 group keeps the ClientHello
inside a single segment and the handshake completes in ~0.1 s.

This is a client-side, path-independent workaround; it does not weaken
certificate verification (the context still uses ``create_default_context()``
and the system trust store).
"""

import logging
import os
import ssl
from typing import Optional, Union

logger = logging.getLogger("qoderroute.tls")

# Escape hatch for operators: any curve name ``SSLContext.set_ecdh_curve``
# accepts, or "default" to keep OpenSSL's own group list.
_CURVE = os.getenv("QODER_TLS_CURVE", "X25519").strip()

_context: Optional[ssl.SSLContext] = None
_context_built = False


def qoder_verify() -> Union[bool, ssl.SSLContext]:
    """Value for ``httpx.AsyncClient(verify=...)`` on Qoder endpoints.

    Returns a context pinned to a single classical curve, or ``True`` (httpx's
    default trust handling) when pinning is disabled/unsupported.
    """
    global _context, _context_built
    if _context_built:
        return _context if _context is not None else True

    if not _CURVE or _CURVE.lower() == "default":
        _context_built = True
        return True

    ctx = ssl.create_default_context()
    try:
        ctx.set_ecdh_curve(_CURVE)
    except Exception as exc:  # noqa: BLE001 - never let TLS policy break startup
        logger.warning(
            "QODER_TLS_CURVE=%s is unusable (%s); falling back to OpenSSL defaults",
            _CURVE,
            exc,
        )
        ctx = None  # type: ignore[assignment]
    else:
        logger.info("pinned outbound TLS group to %s for *.qoder.sh", _CURVE)

    _context = ctx
    _context_built = True
    return _context if _context is not None else True
