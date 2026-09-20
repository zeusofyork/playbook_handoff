"""Campaign discovery: which pools still have money in them.

Headline CPM is the wrong thing to sort on. Budget is pooled per campaign, not
per clipper, so a campaign that stops paying in two days is worth nothing
whatever its rate. This module pulls the public discover board, works out how
fast each pool is draining, and ranks by what is actually left for you.

Two burn-rate estimates, in order of preference:

  observed  the drop in available budget between two of our own snapshots.
            Recent and real, but needs at least two runs.
  lifetime  spend since the campaign was funded, divided by its age. Available
            on the very first run, but it is an average - a pool that has just
            been discovered by a thousand clippers burns far faster than its
            lifetime figure suggests.

The board is fetched from the public /discover page only. The site's
robots.txt disallows /api/, so this never touches it, and responses are cached
so repeated runs do not hammer the origin.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .util import ensure_dir, read_json, write_json

log = logging.getLogger("clipper.campaigns")

BOARD_URL = "https://contentrewards.com/discover"
DISALLOWED_PATHS = ("/api/",)  # per https://contentrewards.com/robots.txt
USER_AGENT = "clipper/0.1 (campaign research; https://github.com/)"
DEFAULT_CACHE_SECONDS = 3600
MS_PER_DAY = 86_400_000.0
SECONDS_PER_DAY = 86_400.0

# Beyond this many days of runway, more makes no practical difference to the
# decision - you will have moved on long before the pool drains.
RUNWAY_PLATEAU_DAYS = 30.0
# Campaigns older than this are treated as having no usable lifetime average.
MIN_AGE_DAYS = 0.5


class BoardError(RuntimeError):
    """The board could not be fetched."""


class BoardParseError(RuntimeError):
    """The board was fetched but no campaigns could be read out of it.

    Raised loudly rather than returning an empty list: a scraper that silently
    finds nothing when the site changes is worse than one that fails.
    """


@dataclass
class Campaign:
    id: str
    title: str
    brand: str
    cpm: float
    budget_total: float
    budget_spent: float
    budget_available: float
    creators: int
    submissions: int
    platforms: list[str]
    category: str
    created_at_ms: int
    requires_application: bool
    verified: bool
    description: str = ""
    experience_id: str = ""

    # Derived by enrich().
    burn_per_day: float | None = None
    burn_source: str = "none"
    days_left: float | None = None
    age_days: float = 0.0
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)

    @property
    def short_id(self) -> str:
        return self.id.split("-")[0]

    @property
    def budget_per_creator(self) -> float:
        """Dollars still in the pool per clipper already competing for it."""
        return self.budget_available / max(1, self.creators)

    @property
    def spent_fraction(self) -> float:
        return self.budget_spent / self.budget_total if self.budget_total else 0.0

    def runway_label(self) -> str:
        if self.days_left is None:
            return "?"
        if self.days_left >= 999:
            return ">999d"
        return f"{self.days_left:.0f}d"


def _first(raw: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in raw and raw[name] is not None:
            return raw[name]
    return default


def campaign_from_raw(raw: dict[str, Any]) -> Campaign:
    return Campaign(
        id=str(_first(raw, "id", default="")),
        title=str(_first(raw, "title", default="(untitled)")),
        brand=str(_first(raw, "brand", default="")),
        cpm=float(_first(raw, "payoutSortRaw", default=0.0) or 0.0),
        budget_total=float(_first(raw, "budgetTotalRaw", default=0.0) or 0.0),
        budget_spent=float(_first(raw, "budgetSpentRaw", default=0.0) or 0.0),
        budget_available=float(_first(raw, "availableBudgetRaw", default=0.0) or 0.0),
        creators=int(_first(raw, "creatorCountRaw", default=0) or 0),
        submissions=int(_first(raw, "submissionCountRaw", default=0) or 0),
        platforms=list(_first(raw, "platforms", default=[]) or []),
        category=str(_first(raw, "category", default="") or ""),
        created_at_ms=int(_first(raw, "createdAtMs", default=0) or 0),
        requires_application=bool(_first(raw, "requiresApplication", default=False)),
        verified=bool(_first(raw, "isVerified", default=False)),
        description=str(_first(raw, "description", default="") or ""),
        experience_id=str(_first(raw, "organizationExperienceId", default="") or ""),
    )


# ------------------------------------------------------------------ fetching


def _check_path_allowed(url: str) -> None:
    for blocked in DISALLOWED_PATHS:
        if blocked in url:
            raise BoardError(f"refusing to fetch {url}: robots.txt disallows {blocked}")


def board_cache_path(work: Path) -> Path:
    return work / "campaigns" / "board.html"


def fetch_board(
    work: Path,
    *,
    url: str = BOARD_URL,
    max_age: float = DEFAULT_CACHE_SECONDS,
    force: bool = False,
    timeout: float = 30.0,
) -> str:
    """Fetch the discover page, reusing a recent cached copy when there is one."""
    _check_path_allowed(url)
    cache = board_cache_path(work)

    if cache.exists() and not force:
        age = time.time() - cache.stat().st_mtime
        if age < max_age:
            log.info("using cached board (%.0f min old; --refresh to force)", age / 60)
            return cache.read_text(encoding="utf-8")

    log.info("fetching %s", url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if cache.exists():
            log.warning("fetch failed (%s); falling back to the cached board", exc)
            return cache.read_text(encoding="utf-8")
        raise BoardError(f"could not fetch {url}: {exc}") from exc

    ensure_dir(cache.parent)
    cache.write_text(body, encoding="utf-8")
    return body


# ------------------------------------------------------------------- parsing

_FLIGHT_CHUNK = re.compile(r"self\.__next_f\.push\(\[\d+,(\".*?\")\]\)", re.S)
_CAMPAIGN_NEEDLE = '{"availableBudgetRaw":'


def _flight_payload(html: str) -> str:
    """Concatenate the Next.js RSC flight chunks into one decoded string."""
    parts: list[str] = []
    for literal in _FLIGHT_CHUNK.findall(html):
        try:
            parts.append(json.loads(literal))
        except json.JSONDecodeError:
            continue
    return "".join(parts)


def parse_board(html: str) -> list[Campaign]:
    """Pull campaign records out of the page's embedded data.

    The board is server-rendered, so the full records - including fields the
    visible table leaves out, like clipper count and funding date - are already
    in the flight payload. No headless browser needed.
    """
    payload = _flight_payload(html)
    if not payload:
        raise BoardParseError(
            "no Next.js flight payload in the page. The site's markup has probably "
            "changed; clipper/campaigns.py needs updating."
        )

    decoder = json.JSONDecoder()
    seen: dict[str, Campaign] = {}
    index = 0
    while True:
        index = payload.find(_CAMPAIGN_NEEDLE, index)
        if index < 0:
            break
        try:
            raw, end = decoder.raw_decode(payload, index)
        except ValueError:
            index += len(_CAMPAIGN_NEEDLE)
            continue
        index = end
        campaign = campaign_from_raw(raw)
        if campaign.id:
            seen[campaign.id] = campaign

    if not seen:
        raise BoardParseError(
            "found the flight payload but no campaign records in it. The board's "
            "field names have probably changed; update campaign_from_raw()."
        )
    log.info("parsed %d campaigns from the board", len(seen))
    return list(seen.values())


# ------------------------------------------------------------------- history


class History:
    """Budget snapshots over time, so burn rate can be observed rather than assumed."""

    MAX_SNAPSHOTS = 200

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, list[dict[str, float]]] = {}
        if path.exists():
            try:
                self.data = read_json(path).get("campaigns", {})
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("history at %s is unreadable (%s); starting fresh", path, exc)

    @classmethod
    def for_work(cls, work: Path) -> "History":
        return cls(ensure_dir(work / "campaigns") / "history.json")

    def record(self, campaigns: Iterable[Campaign], *, now: float | None = None) -> None:
        stamp = time.time() if now is None else now
        for campaign in campaigns:
            series = self.data.setdefault(campaign.id, [])
            if series and stamp - series[-1]["t"] < 60:
                continue  # two runs a minute apart say nothing new
            series.append({"t": stamp, "available": campaign.budget_available})
            del series[: max(0, len(series) - self.MAX_SNAPSHOTS)]

    def observed_burn(self, campaign_id: str, *, now: float | None = None) -> float | None:
        """Dollars per day, from the oldest snapshot that is at least an hour old."""
        series = self.data.get(campaign_id, [])
        if len(series) < 2:
            return None
        stamp = time.time() if now is None else now
        latest = series[-1]
        for earlier in series:
            elapsed = latest["t"] - earlier["t"]
            if elapsed >= 3600:
                drop = earlier["available"] - latest["available"]
                if drop <= 0:
                    return 0.0
                return drop / (elapsed / SECONDS_PER_DAY)
        return None

    def save(self) -> None:
        write_json(self.path, {"version": 1, "updated_at": time.time(), "campaigns": self.data})


class Watchlist:
    """Campaign IDs to alert on when their runway gets short."""

    def __init__(self, path: Path):
        self.path = path
        self.ids: list[str] = []
        if path.exists():
            try:
                self.ids = list(read_json(path).get("ids", []))
            except (json.JSONDecodeError, OSError):
                self.ids = []

    @classmethod
    def for_work(cls, work: Path) -> "Watchlist":
        return cls(ensure_dir(work / "campaigns") / "watch.json")

    def add(self, campaign_id: str) -> bool:
        if campaign_id in self.ids:
            return False
        self.ids.append(campaign_id)
        return True

    def remove(self, campaign_id: str) -> bool:
        if campaign_id not in self.ids:
            return False
        self.ids.remove(campaign_id)
        return True

    def save(self) -> None:
        write_json(self.path, {"version": 1, "ids": self.ids})


# -------------------------------------------------------------------- scoring


def enrich(
    campaigns: Sequence[Campaign], history: History | None = None, *, now: float | None = None
) -> list[Campaign]:
    """Attach burn rate, runway and opportunity score to each campaign."""
    wall = time.time() if now is None else now

    for campaign in campaigns:
        campaign.age_days = max(
            MIN_AGE_DAYS, (wall * 1000.0 - campaign.created_at_ms) / MS_PER_DAY
        )

        burn = history.observed_burn(campaign.id, now=wall) if history else None
        if burn is not None:
            campaign.burn_source = "observed"
        else:
            burn = campaign.budget_spent / campaign.age_days
            campaign.burn_source = "lifetime" if burn > 0 else "none"

        campaign.burn_per_day = burn
        if burn and burn > 0:
            campaign.days_left = campaign.budget_available / burn
        else:
            campaign.days_left = 999.0 if campaign.budget_available > 0 else 0.0

    _score(campaigns)
    return list(campaigns)


def _score(campaigns: Sequence[Campaign]) -> None:
    """Rank by what you can actually earn, not by headline rate.

    Three components, each normalised to 0..1 across the board:

      runway    days of budget left, flattening out at RUNWAY_PLATEAU_DAYS
      rate      CPM relative to the best on the board
      headroom  budget left per clipper already competing, log-scaled because
                the distribution is heavy-tailed

    Weighted toward runway on purpose: a dead pool pays nothing at any rate.
    This is a heuristic for ordering a shortlist, not a forecast.
    """
    if not campaigns:
        return

    best_cpm = max((c.cpm for c in campaigns), default=0.0) or 1.0
    # Percentile rank, not a ratio: dollars-per-clipper is heavy-tailed enough
    # that any linear or log scaling squashes the whole board into a narrow band
    # and the component stops discriminating between campaigns at all.
    headroom = percentile_ranks([c.budget_per_creator for c in campaigns])

    for campaign, room in zip(campaigns, headroom):
        runway = min(1.0, (campaign.days_left or 0.0) / RUNWAY_PLATEAU_DAYS)
        rate = min(1.0, campaign.cpm / best_cpm)

        campaign.components = {
            "runway": round(runway, 3),
            "rate": round(rate, 3),
            "headroom": round(room, 3),
        }
        campaign.score = round(0.45 * runway + 0.25 * rate + 0.30 * room, 4)


def percentile_ranks(values: Sequence[float]) -> list[float]:
    """Fraction of the board each value is greater than or equal to, in 0..1.

    Ties share a rank, and a single-element board scores 1.0.
    """
    count = len(values)
    if count == 0:
        return []
    if count == 1:
        return [1.0]
    ordered = sorted(values)
    ranks = []
    for value in values:
        # Number of entries strictly below, plus half the ties: stable for
        # boards where many campaigns share a budget-per-clipper figure.
        below = sum(1 for other in ordered if other < value)
        ties = sum(1 for other in ordered if other == value)
        ranks.append((below + (ties - 1) / 2.0) / (count - 1))
    return [min(1.0, max(0.0, r)) for r in ranks]


def rank(campaigns: Sequence[Campaign]) -> list[Campaign]:
    return sorted(campaigns, key=lambda c: (-c.score, -c.budget_available))


def filter_campaigns(
    campaigns: Sequence[Campaign],
    *,
    platform: str | None = None,
    min_days: float | None = None,
    min_budget: float | None = None,
    min_cpm: float | None = None,
    max_creators: int | None = None,
    include_application: bool = True,
    query: str | None = None,
) -> list[Campaign]:
    out = []
    for campaign in campaigns:
        if platform and platform.lower() not in [p.lower() for p in campaign.platforms]:
            continue
        if min_days is not None and (campaign.days_left or 0.0) < min_days:
            continue
        if min_budget is not None and campaign.budget_available < min_budget:
            continue
        if min_cpm is not None and campaign.cpm < min_cpm:
            continue
        if max_creators is not None and campaign.creators > max_creators:
            continue
        if not include_application and campaign.requires_application:
            continue
        if query:
            haystack = f"{campaign.title} {campaign.brand} {campaign.category}".lower()
            if query.lower() not in haystack:
                continue
        out.append(campaign)
    return out


def resolve(campaigns: Sequence[Campaign], token: str) -> Campaign:
    """Find one campaign by full ID, ID prefix, or a unique title substring."""
    exact = [c for c in campaigns if c.id == token]
    if exact:
        return exact[0]

    lowered = token.lower()
    matches = [
        c for c in campaigns if c.id.startswith(token) or lowered in c.title.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise KeyError(f"no campaign matching {token!r}")
    raise KeyError(
        f"{token!r} is ambiguous: "
        + ", ".join(f"{c.short_id} ({c.title[:30]})" for c in matches[:5])
    )


def to_dict(campaign: Campaign) -> dict[str, Any]:
    data = asdict(campaign)
    data["budget_per_creator"] = round(campaign.budget_per_creator, 2)
    data["spent_fraction"] = round(campaign.spent_fraction, 4)
    return data
