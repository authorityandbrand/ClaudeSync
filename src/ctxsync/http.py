"""Shared HTTP helpers for provider modules.

Every REST caller we ship (claude.ai-kv, Gemini_Gws MCP, native Drive REST)
goes through :func:`urlopen_with_retry`, so a single place decides how we
handle transient upstream failures.

The retry policy is intentionally conservative: 3 total tries, backing off
1s -> 2s between attempts, and *only* on the status codes that generally
mean "come back later" (``429`` and 5xx-of-the-``50[234]``-family). ``500``
is excluded on purpose — a bare 500 is usually a real server bug and
retrying it hides the signal.

``Retry-After`` on the failed response is honored when present (seconds only;
HTTP-date form is treated as absent, since datacenter clocks drift and we
prefer the exponential fallback over guessing).
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Callable, Optional, Sequence

# Codes we retry. 500 deliberately not included — see module docstring.
RETRY_STATUS: Sequence[int] = (429, 502, 503, 504)
DEFAULT_MAX_TRIES = 3
DEFAULT_TIMEOUT = 45


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Return the numeric seconds in a Retry-After header, or None."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def urlopen_with_retry(
    req: urllib.request.Request,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    max_tries: int = DEFAULT_MAX_TRIES,
    retry_status: Sequence[int] = RETRY_STATUS,
    sleeper: Callable[[float], None] = time.sleep,
):
    """``urllib.request.urlopen`` with retry on 429 / 502 / 503 / 504.

    Any other HTTP status (or URLError) surfaces on the first hit — retrying
    a 401 or a 400 masks the real cause.
    """
    last_error: Optional[urllib.error.HTTPError] = None
    for attempt in range(max_tries):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code not in retry_status or attempt >= max_tries - 1:
                raise
            retry_after = _parse_retry_after(
                e.headers.get("Retry-After") if e.headers else None
            )
            delay = retry_after if retry_after is not None else float(2 ** attempt)
            last_error = e
            sleeper(delay)
    # Defensive: the loop always either returns or raises above.
    if last_error is not None:  # pragma: no cover
        raise last_error
    raise RuntimeError("urlopen_with_retry: unreachable")  # pragma: no cover
