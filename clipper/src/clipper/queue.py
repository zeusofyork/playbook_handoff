"""Stage 5: the staging queue and posting ledger.

This is deliberately where automation stops. Posting via browser automation
violates TikTok's and Instagram's terms and gets accounts banned, which ends
the whole operation - so the pipeline hands you a folder per clip and tracks
what you posted, where, and what it earned.

The ledger is a JSON file so it diffs, greps and survives being copied around.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .brief import Brief
from .render import Clip
from .util import ensure_dir

log = logging.getLogger("clipper.queue")

STATUS_PENDING = "pending"
STATUS_POSTED = "posted"
STATUS_REJECTED = "rejected"
STATUS_PAID = "paid"
STATUSES = (STATUS_PENDING, STATUS_POSTED, STATUS_REJECTED, STATUS_PAID)


@dataclass
class Posting:
    platform: str
    url: str
    posted_at: float = field(default_factory=time.time)
    views: int = 0
    payout_usd: float = 0.0


@dataclass
class Entry:
    clip_id: str
    campaign: str
    path: str
    caption: str
    hook: str
    duration: float
    score: float
    status: str = STATUS_PENDING
    note: str = ""
    added_at: float = field(default_factory=time.time)
    postings: list[Posting] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Entry":
        postings = [Posting(**p) for p in raw.pop("postings", [])]
        return cls(**raw, postings=postings)

    def total_views(self) -> int:
        return sum(p.views for p in self.postings)

    def total_payout(self) -> float:
        return sum(p.payout_usd for p in self.postings)


class Ledger:
    """Append-and-update store of every clip the pipeline has produced."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, Entry] = {}
        self._load()

    @classmethod
    def for_work(cls, work: Path) -> "Ledger":
        return cls(ensure_dir(work / "queue") / "ledger.json")

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for item in raw.get("entries", []):
            entry = Entry.from_raw(dict(item))
            self.entries[entry.clip_id] = entry

    def save(self) -> None:
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "entries": [asdict(e) for e in self.sorted_entries()],
        }
        ensure_dir(self.path.parent)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def sorted_entries(self) -> list[Entry]:
        return sorted(self.entries.values(), key=lambda e: (e.added_at, e.clip_id))

    def add(self, clip: Clip) -> Entry:
        existing = self.entries.get(clip.clip_id)
        if existing:
            # Re-rendering an existing clip must not wipe its posting history.
            existing.caption = clip.caption
            existing.hook = clip.hook
            existing.path = clip.path
            return existing
        entry = Entry(
            clip_id=clip.clip_id,
            campaign=clip.campaign,
            path=clip.path,
            caption=clip.caption,
            hook=clip.hook,
            duration=clip.duration,
            score=clip.score,
        )
        self.entries[entry.clip_id] = entry
        return entry

    def get(self, clip_id: str) -> Entry:
        entry = self.entries.get(clip_id)
        if entry is None:
            matches = [e for cid, e in self.entries.items() if cid.startswith(clip_id)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise KeyError(f"{clip_id!r} is ambiguous: {', '.join(m.clip_id for m in matches)}")
            raise KeyError(f"no clip {clip_id!r} in the ledger")
        return entry

    def mark_posted(self, clip_id: str, platform: str, url: str) -> Entry:
        entry = self.get(clip_id)
        if any(p.platform == platform and p.url == url for p in entry.postings):
            log.info("%s already recorded on %s", entry.clip_id, platform)
            return entry
        entry.postings.append(Posting(platform=platform, url=url))
        entry.status = STATUS_POSTED
        return entry

    def mark_rejected(self, clip_id: str, note: str = "") -> Entry:
        entry = self.get(clip_id)
        entry.status = STATUS_REJECTED
        entry.note = note
        return entry

    def record_views(self, clip_id: str, platform: str, views: int, brief: Brief) -> Entry:
        """Update view count for a posting and recompute its estimated payout."""
        entry = self.get(clip_id)
        posting = next((p for p in entry.postings if p.platform == platform), None)
        if posting is None:
            raise KeyError(f"{entry.clip_id} has no posting on {platform!r}")
        posting.views = views
        posting.payout_usd = estimate_payout(views, brief)
        return entry

    def pending(self) -> list[Entry]:
        return [e for e in self.sorted_entries() if e.status == STATUS_PENDING]

    def stats(self) -> dict[str, Any]:
        entries = self.sorted_entries()
        by_status = {s: 0 for s in STATUSES}
        for entry in entries:
            by_status[entry.status] = by_status.get(entry.status, 0) + 1
        views = sum(e.total_views() for e in entries)
        payout = sum(e.total_payout() for e in entries)
        posted = [e for e in entries if e.postings]
        return {
            "clips": len(entries),
            "by_status": by_status,
            "posted": len(posted),
            "total_views": views,
            "estimated_payout_usd": round(payout, 2),
            "median_views": _median([e.total_views() for e in posted]),
            "effective_cpm_usd": round(payout / (views / 1000.0), 3) if views else 0.0,
        }


def estimate_payout(views: int, brief: Brief) -> float:
    """Estimated gross for one posting. An estimate, not a promise."""
    econ = brief.economics
    if views < econ.min_views_to_qualify:
        return 0.0
    gross = (views / 1000.0) * econ.rate_per_1k_views_usd
    if econ.per_clip_cap_usd is not None:
        gross = min(gross, econ.per_clip_cap_usd)
    return round(gross, 2)


def _median(values: Iterable[int]) -> float:
    data = sorted(values)
    if not data:
        return 0.0
    mid = len(data) // 2
    if len(data) % 2:
        return float(data[mid])
    return (data[mid - 1] + data[mid]) / 2.0


def write_checklist(entry: Entry, brief: Brief, out_dir: Path) -> Path:
    """A per-clip POST.md so the manual step is mechanical, not a memory test."""
    platforms = "\n".join(f"- [ ] {p}" for p in brief.platforms)
    body = f"""# Post checklist - {entry.clip_id}

Campaign: {brief.name}
Clip: {entry.path}
Length: {entry.duration:.1f}s   Score: {entry.score:.3f}

## Caption

```
{entry.caption}
```

## Post to

{platforms}

## Before you hit publish

- [ ] Watch the first 2 seconds with sound off - does the hook land?
- [ ] Captions readable, not clipped by the platform's UI safe area
- [ ] Brief rules re-read (duration, attribution, banned topics)
- [ ] Posted manually from the app or an official API - never a browser bot

## After

```
clipper queue posted {entry.clip_id} --platform <platform> --url <url>
clipper queue views  {entry.clip_id} --platform <platform> --views <n>
```
"""
    path = out_dir / "POST.md"
    path.write_text(body, encoding="utf-8")
    return path
