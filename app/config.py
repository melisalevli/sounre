import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    contact: str  # MusicBrainz requires a way to reach you in the User-Agent
    cache_dir: str = ".cache"
    cache_size_bytes: int = 256 * 1024**2
    mb_interval: float = 1.05  # MusicBrainz allows ~1 request/second per IP
    spotify_id: str | None = None  # optional: enables the "exists on Spotify" check
    spotify_secret: str | None = None
    ip_budget: int = 120  # request-cost points per minute per IP (see app/abuse.py)
    proxy_hops: int = 1  # for proxy="forwarded": which X-Forwarded-For entry from the right is the visitor
    proxy: str = "none"  # where the visitor IP comes from: none | cloudflare | forwarded (see app/abuse.py)

    @property
    def user_agent(self) -> str:
        return f"Sounre/0.1 ({self.contact})"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            contact=os.environ["SOUNRE_CONTACT"],
            cache_dir=os.getenv("SOUNRE_CACHE_DIR", cls.cache_dir),
            spotify_id=os.getenv("SPOTIFY_CLIENT_ID"),
            spotify_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
            ip_budget=int(os.getenv("SOUNRE_IP_BUDGET", cls.ip_budget)),
            proxy=os.getenv("SOUNRE_PROXY", cls.proxy),
            proxy_hops=int(os.getenv("SOUNRE_PROXY_HOPS", cls.proxy_hops)),
        )