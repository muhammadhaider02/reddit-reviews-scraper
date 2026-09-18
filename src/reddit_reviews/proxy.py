"""Residential proxy plumbing, shared by the browser and the mobile route.

It lives in its own module because BOTH routes need it and `mobile` must not import `scraper` -
that would be a cycle. It moved here on 18 Sep 2026, when Reddit blocked this host's IP from
`oauth.reddit.com` outright and the mobile calls had to start going through the proxy as well.

The port pool is the reason this is shared rather than duplicated: one pool across the whole
process is what guarantees two concurrent fetches never leave from the same exit IP, whichever
route they belong to.
"""

from __future__ import annotations

import logging
import random
import threading
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlsplit

from .config import settings

log = logging.getLogger("reddit_reviews.proxy")


def proxy_url(port: int | None = None) -> str | None:
    """The same credential as a URL, for stdlib HTTP clients that cannot take a dict.

    `proxy_config` returns Playwright's dict because urlparse does no percent-decoding and a
    password containing '%', '@' or ':' would authenticate wrongly. urllib needs a URL, so the
    credential is percent-ENCODED here rather than interpolated raw - the same hazard, handled at
    the other end.
    """
    conf = proxy_config(port)
    if not conf:
        return None
    if isinstance(conf, str):
        return conf
    from urllib.parse import quote

    scheme, _, hostport = conf["server"].partition("://")
    creds = f"{quote(conf['username'], safe='')}:{quote(conf['password'], safe='')}"
    return f"{scheme}://{creds}@{hostport}"


STICKY_PORT_RANGE = (10001, 10100)
# Every residential vendor publishes the same pool on two kinds of port, and the rotating one is
# always the default in their dashboard - so this is the mistake that gets made, not a hypothetical.
# Keyed by port because that is all the credential tells us. Decodo 7000, DataImpulse 823. Both
# vendors' sticky ranges cover STICKY_PORT_RANGE (Decodo 10001-10099, DataImpulse 10000-20000), so
# the pool above needs no per-vendor handling.
ROTATING_GATEWAY_PORTS = {"7000": "Decodo", "823": "DataImpulse"}


def proxy_config(port: int | None = None) -> dict[str, str] | str | None:
    """SCRAPER_PROXY as Scrapling wants it, with `port` overriding the configured one.

    Two accepted forms. Anything containing '://' is a plain proxy URL and is passed through
    untouched (no rotation applies). Otherwise it is Decodo's four-field `host:port:user:pass`,
    which is split at most THREE times because the password may itself contain ':' - everything
    after the third colon belongs to it.

    Returns Playwright's proxy dict rather than a URL on purpose. Scrapling would accept a URL, but
    it parses one with urlparse, which does no percent-decoding: a password containing '%', '@' or
    ':' would authenticate with the wrong value or fail to parse outright. The dict does no string
    surgery at all.
    """
    raw = (settings.proxy or "").strip()
    if not raw:
        return None
    if "://" in raw:
        # A plain proxy URL, passed through untouched - but still checked, because the guard below is
        # the whole point of this function and a URL is the easy way to walk straight past it. This
        # is not hypothetical: DataImpulse hands out `user:pass@host:823` in exactly this shape.
        _reject_rotating_gateway(urlsplit(raw).port)
        return raw
    # Deliberately unguarded: a credential with fewer than four fields raises ValueError here, and a
    # config fault should stop the scrape rather than silently let it run from the blocked server IP.
    host, configured, user, pw = raw.split(":", 3)
    _reject_rotating_gateway(configured)
    # http:// and not socks5://: Chromium ignores SOCKS credentials, so an authenticated socks5 proxy
    # silently drops the username and password and the gateway refuses the connection.
    return {"server": f"http://{host}:{port if port is not None else configured}", "username": user, "password": pw}


def _reject_rotating_gateway(port) -> None:
    """Refuse a rotating gateway loudly, at config time.

    A rotating gateway hands out a new exit IP per REQUEST. Search follows Reddit's cursor across
    several pages as one session, so rotating would give Reddit a different address for every page of
    one scrape - which is the pattern that gets a session refused. The failure it causes is a bad one
    to debug, because it looks exactly like being blocked.
    """
    vendor = ROTATING_GATEWAY_PORTS.get(str(port or ""))
    if vendor:
        raise ValueError(
            f"SCRAPER_PROXY points at {vendor}'s rotating gateway (port {port}). Use a sticky port "
            f"({STICKY_PORT_RANGE[0]}): one exit IP has to serve a whole fetch, and the cursor pages "
            f"after it."
        )


class _PortPool:
    """Hands each in-flight fetch a sticky port no other in-flight fetch is holding.

    Two browsers leaving from one residential exit at the same time is a fingerprint, and drawing a
    port at random per call is not enough to prevent it - independent draws collide. Ports are held
    for exactly as long as the browser using them, so with MAX_CONCURRENCY permits against ~99 ports
    this can never exhaust.
    """

    def __init__(self, lo: int, hi: int) -> None:
        self._free = set(range(lo, hi))
        self._lock = threading.Lock()

    def acquire(self) -> int | None:
        """Take a port and hold it until release(). Used by the mobile route, where a device keeps
        one exit for its whole life rather than for the span of a single fetch."""
        with self._lock:
            # Random rather than sequential so repeated runs do not keep re-drawing the same exits.
            port = random.choice(sorted(self._free)) if self._free else None
            self._free.discard(port)
        return port

    def release(self, port: int | None) -> None:
        if port is not None:
            with self._lock:
                self._free.add(port)

    @contextmanager
    def lease(self) -> Iterator[int | None]:
        port = self.acquire()
        try:
            yield port
        finally:
            self.release(port)


_port_pool = _PortPool(*STICKY_PORT_RANGE)

