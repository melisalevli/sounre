"""Per-IP request budget and security headers as one pure-ASGI middleware.

Pure ASGI on purpose: Starlette's BaseHTTPMiddleware would wrap `receive`, and api/guard.py relies on
`Request.is_disconnected()` to cancel abandoned work.
"""
import math
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from .i18n import message

# What one call can spend upstream. Cache misses share MusicBrainz's 1 request/s for the whole server.
COST = {
    "/api/v1/search": 4, "/api/v1/analyze": 4, "/api/v1/similar": 3,
    "/api/v1/similar-genres": 3, "/api/v1/cover": 1,
}
HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"content-security-policy", b"frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'"),
]


class Guard:
    """Token bucket per IP: `budget` points refill over one minute; each endpoint costs `COST` points.

    `proxy` says where the visitor's IP comes from, and must match how the app is really reached:
      none        the socket address (running locally, or directly on the internet)
      cloudflare  the CF-Connecting-IP header (only safe if Cloudflare is the sole way in)
      forwarded   the `hops`-th X-Forwarded-For entry counted from the right (1 = last). Proxies only append, so
                  entries on the left are visitor-controlled; the right-hand ones were added by the platform
    A header the visitor can forge must never be trusted, or the limit is trivial to dodge.
    """

    def __init__(self, app, budget: int = 120, proxy: str = "none", hops: int = 1):
        if proxy not in ("none", "cloudflare", "forwarded"):
            raise ValueError(f"unknown proxy mode: {proxy}")
        if hops < 1:
            raise ValueError("hops must be at least 1")
        self.app, self.budget, self.proxy, self.hops = app, budget, proxy, hops
        self.rate = budget / 60  # points per second
        self.buckets: dict[str, tuple[float, float]] = {}  # ip -> (points, last seen)

    def _ip(self, scope) -> str:
        wanted = {"cloudflare": b"cf-connecting-ip", "forwarded": b"x-forwarded-for"}.get(self.proxy)
        if wanted:
            for name, value in scope["headers"]:
                if name != wanted:
                    continue
                parts = [p.strip() for p in value.decode("latin-1").split(",")]
                if self.proxy == "cloudflare":
                    parts = parts[-1:]  # a single address; take the last to be safe
                if len(parts) >= (1 if self.proxy == "cloudflare" else self.hops):
                    ip = parts[-(1 if self.proxy == "cloudflare" else self.hops)]
                    if ip:
                        return ip
        return (scope.get("client") or ("?", 0))[0]

    def _take(self, ip: str, cost: int) -> float:
        """0 when allowed (points are spent), else the seconds until `cost` points are back."""
        now = time.monotonic()
        if len(self.buckets) > 10_000:  # forget idle visitors: a full bucket equals no bucket
            self.buckets = {k: b for k, b in self.buckets.items() if b[0] + (now - b[1]) * self.rate < self.budget}
        points, last = self.buckets.get(ip, (self.budget, now))
        points = min(self.budget, points + (now - last) * self.rate)
        if points >= cost:
            self.buckets[ip] = (points - cost, now)
            return 0.0
        self.buckets[ip] = (points, now)
        return (cost - points) / self.rate

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(msg):
            if msg["type"] == "http.response.start":
                msg["headers"] = [*msg.get("headers", []), *HEADERS]
            await send(msg)

        if (cost := COST.get(scope["path"])) and (wait := self._take(self._ip(scope), cost)):
            lang = Request(scope).query_params.get("lang")
            reply = JSONResponse({"detail": message("rate_limited", lang)}, 429, {"Retry-After": str(math.ceil(wait))})
            return await reply(scope, receive, send_with_headers)
        await self.app(scope, receive, send_with_headers)
