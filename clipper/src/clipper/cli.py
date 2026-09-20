"""Command line entry point.

    clipper doctor                 check the external tools this needs
    clipper init ./campaigns/foo   scaffold a campaign brief
    clipper run --brief b.yml      fetch -> transcribe -> score -> render -> queue
    clipper queue list             what is staged and waiting for you to post
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import textwrap
from pathlib import Path

from . import __version__
from .brief import Brief, BriefError
from .campaigns import (
    BoardError,
    BoardParseError,
    Campaign,
    History,
    Watchlist,
    enrich,
    fetch_board,
    filter_campaigns,
    parse_board,
    rank,
    resolve,
    to_dict,
)
from .fetch import fetch
from .queue import Ledger, write_checklist
from .render import BriefViolation, render_clip
from .score import select_moments
from .transcribe import load_transcript, transcribe
from .util import ToolMissing, CommandFailed, ensure_dir, fmt_timestamp, setup_logging

log = logging.getLogger("clipper.cli")

TEMPLATE = Path(__file__).parent / "templates" / "campaign.yml"


def load_brief(args: argparse.Namespace) -> Brief:
    path = Path(args.brief)
    if not path.exists():
        raise SystemExit(f"brief not found: {path}\nRun `clipper init <dir>` to scaffold one.")
    return Brief.from_yaml(path)


def origins(brief: Brief, args: argparse.Namespace) -> list[str]:
    urls = list(args.url) if getattr(args, "url", None) else list(brief.source_urls)
    if not urls:
        raise SystemExit("no sources: add source_urls to the brief or pass --url")
    placeholders = [u for u in urls if "REPLACE_ME" in u]
    if placeholders:
        raise SystemExit(
            "the brief still has scaffolded placeholders in source_urls:\n  "
            + "\n  ".join(placeholders)
            + "\nPoint them at content the campaign licensed you to clip."
        )
    return urls


# --------------------------------------------------------------------------- commands


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = [
        ("ffmpeg", "render + audio analysis", "apt install ffmpeg / brew install ffmpeg"),
        ("ffprobe", "media probing", "ships with ffmpeg"),
        ("yt-dlp", "fetching remote sources", "pip install yt-dlp"),
    ]
    ok = True
    for binary, why, hint in checks:
        found = shutil.which(binary)
        print(f"{'OK  ' if found else 'MISS'}  {binary:<10} {why:<28} {found or hint}")
        ok = ok and bool(found)

    try:
        import faster_whisper  # noqa: F401

        print(f"{'OK  '}  {'whisper':<10} {'transcription + captions':<28} faster-whisper installed")
    except ImportError:
        print(
            f"{'MISS'}  {'whisper':<10} {'transcription + captions':<28} "
            "pip install 'clipper[whisper]'"
        )
        ok = False

    print()
    print("All green" if ok else "Missing pieces above; --no-transcript still works without whisper.")
    return 0 if ok else 1


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.directory)
    ensure_dir(target)
    dest = target / "campaign.yml"
    if dest.exists() and not args.force:
        raise SystemExit(f"{dest} already exists (use --force to overwrite)")
    shutil.copyfile(TEMPLATE, dest)
    print(f"wrote {dest}")
    print("Edit it against the campaign's own brief, then:")
    print(f"  clipper run --brief {dest}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    brief = load_brief(args)
    work = Path(args.work)
    for origin in origins(brief, args):
        src = fetch(origin, work, max_height=args.max_height, cookies=args.cookies, force=args.force)
        print(f"{src.source_id}  {fmt_timestamp(src.duration)}  {src.title}")
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    brief = load_brief(args)
    work = Path(args.work)
    for origin in origins(brief, args):
        src = fetch(origin, work, max_height=args.max_height, cookies=args.cookies)
        data = transcribe(
            src,
            model=brief.whisper_model,
            device=brief.whisper_device,
            compute_type=brief.whisper_compute_type,
            force=args.force,
        )
        print(f"{src.source_id}  {len(data['segments'])} segments  {data['language']}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    brief = load_brief(args)
    work = Path(args.work)
    for origin in origins(brief, args):
        src = fetch(origin, work, max_height=args.max_height, cookies=args.cookies)
        transcript = None
        if not args.no_transcript:
            transcript = transcribe(
                src,
                model=brief.whisper_model,
                device=brief.whisper_device,
                compute_type=brief.whisper_compute_type,
            )
        moments = select_moments(src, brief, transcript, force=args.force)
        if args.json:
            print(json.dumps([m.__dict__ for m in moments], indent=2))
        else:
            for i, moment in enumerate(moments, 1):
                print(f"{i:>2}. {moment.label()}  {moment.text[:60]}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    brief = load_brief(args)
    work = Path(args.work)
    ledger = Ledger.for_work(work)
    rendered = skipped = 0

    for origin in origins(brief, args):
        src = fetch(origin, work, max_height=args.max_height, cookies=args.cookies)
        transcript = load_transcript(src)
        if transcript is None and not args.no_transcript:
            transcript = transcribe(
                src,
                model=brief.whisper_model,
                device=brief.whisper_device,
                compute_type=brief.whisper_compute_type,
            )
        moments = select_moments(src, brief, transcript)
        if args.top:
            moments = sorted(moments, key=lambda m: m.score, reverse=True)[: args.top]

        for moment in moments:
            try:
                clip = render_clip(
                    src, moment, brief, work, transcript, dry_run=args.dry_run, force=args.force
                )
            except BriefViolation as exc:
                log.warning("skipped %s: %s", moment.label(), exc)
                skipped += 1
                continue
            rendered += 1
            if not args.dry_run:
                entry = ledger.add(clip)
                write_checklist(entry, brief, Path(clip.path).parent)

    if not args.dry_run:
        ledger.save()
    print(f"\n{rendered} clip(s) ready, {skipped} skipped for brief violations")
    if rendered and not args.dry_run:
        print(f"Staged in {work / 'clips'} - review each POST.md, then post by hand.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    for step in (cmd_fetch, cmd_render):
        rc = step(args)
        if rc:
            return rc
    return 0


def cmd_queue_list(args: argparse.Namespace) -> int:
    ledger = Ledger.for_work(Path(args.work))
    entries = ledger.sorted_entries()
    if args.status:
        entries = [e for e in entries if e.status == args.status]
    if not entries:
        print("queue is empty")
        return 0
    print(f"{'CLIP':<14}{'STATUS':<10}{'LEN':>6}{'SCORE':>7}{'VIEWS':>9}{'EST $':>8}  HOOK")
    for entry in entries:
        print(
            f"{entry.clip_id:<14}{entry.status:<10}{entry.duration:>5.0f}s"
            f"{entry.score:>7.3f}{entry.total_views():>9}{entry.total_payout():>8.2f}  "
            f"{entry.hook[:40]}"
        )
    return 0


def cmd_queue_posted(args: argparse.Namespace) -> int:
    ledger = Ledger.for_work(Path(args.work))
    entry = ledger.mark_posted(args.clip_id, args.platform, args.url)
    ledger.save()
    print(f"{entry.clip_id} -> posted on {args.platform}")
    return 0


def cmd_queue_views(args: argparse.Namespace) -> int:
    brief = load_brief(args)
    ledger = Ledger.for_work(Path(args.work))
    entry = ledger.record_views(args.clip_id, args.platform, args.views, brief)
    ledger.save()
    print(f"{entry.clip_id} -> {args.views} views on {args.platform}, est ${entry.total_payout():.2f}")
    return 0


def cmd_queue_reject(args: argparse.Namespace) -> int:
    ledger = Ledger.for_work(Path(args.work))
    entry = ledger.mark_rejected(args.clip_id, args.note)
    ledger.save()
    print(f"{entry.clip_id} -> rejected ({args.note or 'no reason given'})")
    return 0


def cmd_queue_stats(args: argparse.Namespace) -> int:
    ledger = Ledger.for_work(Path(args.work))
    stats = ledger.stats()
    width = max(len(k) for k in stats)
    for key, value in stats.items():
        print(f"{key:<{width}}  {value}")
    return 0


# ------------------------------------------------------------------ campaigns


def money(amount: float) -> str:
    """Compact dollars: $67.4k, $950, $1.2M."""
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.1f}k"
    return f"${amount:.0f}"


def load_campaigns(args: argparse.Namespace) -> tuple[list[Campaign], History, Watchlist]:
    """Fetch (or reuse) the board, record a snapshot, and enrich every campaign."""
    work = Path(args.work)
    html = fetch_board(
        work,
        max_age=0 if getattr(args, "refresh", False) else args.cache_seconds,
        force=getattr(args, "refresh", False),
    )
    campaigns = parse_board(html)

    history = History.for_work(work)
    watchlist = Watchlist.for_work(work)
    if not getattr(args, "no_record", False):
        history.record(campaigns)
        history.save()

    enrich(campaigns, history)
    return campaigns, history, watchlist


def cmd_campaigns_list(args: argparse.Namespace) -> int:
    campaigns, _, watchlist = load_campaigns(args)
    selected = filter_campaigns(
        campaigns,
        platform=args.platform,
        min_days=args.min_days,
        min_budget=args.min_budget,
        min_cpm=args.min_cpm,
        max_creators=args.max_creators,
        include_application=not args.no_application,
        query=args.query,
    )
    ordered = rank(selected)[: args.limit]

    if args.json:
        print(json.dumps([to_dict(c) for c in ordered], indent=2))
        return 0

    if not ordered:
        print(f"no campaigns matched (board has {len(campaigns)})")
        return 0

    print(
        f"{'ID':<10}{'CPM':>6}{'LEFT':>9}{'BURN/D':>9}{'RUNWAY':>8}"
        f"{'CLIPPERS':>10}{'$/CLIP':>9}{'SCORE':>7}  CAMPAIGN"
    )
    for campaign in ordered:
        flags = "*" if campaign.id in watchlist.ids else " "
        flags += "+" if campaign.requires_application else " "
        print(
            f"{campaign.short_id:<10}"
            f"{'$' + format(campaign.cpm, '.2f'):>6}"
            f"{money(campaign.budget_available):>9}"
            f"{money(campaign.burn_per_day or 0):>9}"
            f"{campaign.runway_label():>8}"
            f"{campaign.creators:>10}"
            f"{money(campaign.budget_per_creator):>9}"
            f"{campaign.score:>7.3f}"
            f"  {flags}{campaign.title[:44]}"
        )

    sources = {c.burn_source for c in ordered}
    print(f"\n{len(ordered)} of {len(campaigns)} campaigns.  * watched   + application required")
    print("Burn rate: " + "/".join(sorted(sources)) + ".")
    if "lifetime" in sources:
        print(
            "  'lifetime' averages spend since funding, so it understates a pool that only"
            " just got busy. Re-run tomorrow for an observed rate."
        )
    print(
        "Score weights runway 0.45, rate 0.25, headroom 0.30."
        " A shortlist heuristic, not a forecast - verify on the campaign page before clipping."
    )
    return 0


def cmd_campaigns_show(args: argparse.Namespace) -> int:
    campaigns, _, watchlist = load_campaigns(args)
    campaign = resolve(campaigns, args.campaign)

    if args.json:
        print(json.dumps(to_dict(campaign), indent=2))
        return 0

    watched = " (watched)" if campaign.id in watchlist.ids else ""
    print(f"{campaign.title}{watched}")
    print(f"  brand         {campaign.brand or '-'}")
    print(f"  id            {campaign.id}")
    print(f"  experience    {campaign.experience_id or '-'}")
    print(f"  category      {campaign.category or '-'}")
    print(f"  platforms     {', '.join(campaign.platforms) or '-'}")
    print(f"  verified      {campaign.verified}   application required: {campaign.requires_application}")
    print()
    print(f"  rate          ${campaign.cpm:.2f} per 1k views")
    print(f"  budget        {money(campaign.budget_total)} total, "
          f"{money(campaign.budget_spent)} spent ({campaign.spent_fraction:.0%}), "
          f"{money(campaign.budget_available)} left")
    print(f"  burn          {money(campaign.burn_per_day or 0)}/day ({campaign.burn_source})")
    print(f"  runway        {campaign.runway_label()} at that rate")
    print(f"  age           {campaign.age_days:.1f} days")
    print()
    print(f"  clippers      {campaign.creators}  ({campaign.submissions} submissions)")
    print(f"  headroom      {money(campaign.budget_per_creator)} left per clipper already in")
    print(f"  score         {campaign.score:.3f}  {campaign.components}")
    if campaign.description:
        print()
        print("  brief:")
        for line in textwrap.wrap(campaign.description, width=88):
            print(f"    {line}")
    print()
    print("  Verify the budget on the campaign page before cutting - this board moves daily.")
    return 0


def cmd_campaigns_watch(args: argparse.Namespace) -> int:
    campaigns, _, watchlist = load_campaigns(args)
    campaign = resolve(campaigns, args.campaign)
    if watchlist.add(campaign.id):
        watchlist.save()
        print(f"watching {campaign.short_id}  {campaign.title}")
    else:
        print(f"already watching {campaign.short_id}  {campaign.title}")
    return 0


def cmd_campaigns_unwatch(args: argparse.Namespace) -> int:
    work = Path(args.work)
    watchlist = Watchlist.for_work(work)
    matches = [i for i in watchlist.ids if i.startswith(args.campaign)]
    if not matches:
        print(f"not watching anything matching {args.campaign!r}", file=sys.stderr)
        return 1
    for campaign_id in matches:
        watchlist.remove(campaign_id)
    watchlist.save()
    print(f"stopped watching {', '.join(i.split('-')[0] for i in matches)}")
    return 0


def cmd_campaigns_check(args: argparse.Namespace) -> int:
    """Exit non-zero when a watched campaign is running out. Built for cron."""
    campaigns, _, watchlist = load_campaigns(args)
    if not watchlist.ids:
        print("nothing on the watchlist; add one with `clipper campaigns watch <id>`")
        return 0

    by_id = {c.id: c for c in campaigns}
    alerts: list[str] = []
    for campaign_id in watchlist.ids:
        campaign = by_id.get(campaign_id)
        if campaign is None:
            alerts.append(f"{campaign_id.split('-')[0]}  GONE from the board (ended or delisted)")
            continue
        if (campaign.days_left or 0.0) < args.min_days:
            alerts.append(
                f"{campaign.short_id}  {campaign.runway_label()} left "
                f"({money(campaign.budget_available)} at {money(campaign.burn_per_day or 0)}/day)"
                f"  {campaign.title[:40]}"
            )

    if not alerts:
        print(f"all {len(watchlist.ids)} watched campaign(s) above {args.min_days:g} days of runway")
        return 0

    print(f"{len(alerts)} watched campaign(s) under {args.min_days:g} days:")
    for alert in alerts:
        print(f"  {alert}")
    return 1



# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clipper", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"clipper {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--work", default="work", help="work directory (default: ./work)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_source_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--brief", required=True, help="path to campaign.yml")
        p.add_argument("--url", action="append", help="override brief source_urls (repeatable)")
        p.add_argument("--max-height", type=int, default=1080, help="cap download resolution")
        p.add_argument("--cookies", type=Path, help="cookies.txt for gated sources")
        p.add_argument("--force", action="store_true", help="redo this stage, ignoring the cache")

    p_doctor = sub.add_parser("doctor", help="check external tool availability")
    p_doctor.set_defaults(func=cmd_doctor)

    p_init = sub.add_parser("init", help="scaffold a campaign brief")
    p_init.add_argument("directory", help="where to write campaign.yml")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_fetch = sub.add_parser("fetch", help="download source video")
    add_source_flags(p_fetch)
    p_fetch.set_defaults(func=cmd_fetch)

    p_tr = sub.add_parser("transcribe", help="word-level transcription")
    add_source_flags(p_tr)
    p_tr.set_defaults(func=cmd_transcribe)

    p_score = sub.add_parser("score", help="rank candidate moments")
    add_source_flags(p_score)
    p_score.add_argument("--no-transcript", action="store_true", help="audio energy only")
    p_score.add_argument("--json", action="store_true")
    p_score.set_defaults(func=cmd_score)

    p_render = sub.add_parser("render", help="cut, caption and encode clips")
    add_source_flags(p_render)
    p_render.add_argument("--no-transcript", action="store_true")
    p_render.add_argument("--top", type=int, help="render only the N highest scoring moments")
    p_render.add_argument("--dry-run", action="store_true", help="print ffmpeg commands only")
    p_render.set_defaults(func=cmd_render)

    p_run = sub.add_parser("run", help="fetch + transcribe + score + render + queue")
    add_source_flags(p_run)
    p_run.add_argument("--no-transcript", action="store_true")
    p_run.add_argument("--top", type=int)
    p_run.add_argument("--dry-run", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_queue = sub.add_parser("queue", help="staging queue and posting ledger")
    qsub = p_queue.add_subparsers(dest="queue_command", required=True)

    q_list = qsub.add_parser("list", help="show staged clips")
    q_list.add_argument("--status", choices=["pending", "posted", "rejected", "paid"])
    q_list.set_defaults(func=cmd_queue_list)

    q_posted = qsub.add_parser("posted", help="record that you posted a clip")
    q_posted.add_argument("clip_id")
    q_posted.add_argument("--platform", required=True)
    q_posted.add_argument("--url", required=True)
    q_posted.set_defaults(func=cmd_queue_posted)

    q_views = qsub.add_parser("views", help="record view count and estimate payout")
    q_views.add_argument("clip_id")
    q_views.add_argument("--platform", required=True)
    q_views.add_argument("--views", type=int, required=True)
    q_views.add_argument("--brief", required=True, help="brief supplying the payout terms")
    q_views.set_defaults(func=cmd_queue_views)

    q_reject = qsub.add_parser("reject", help="mark a clip as not worth posting")
    q_reject.add_argument("clip_id")
    q_reject.add_argument("--note", default="")
    q_reject.set_defaults(func=cmd_queue_reject)

    q_stats = qsub.add_parser("stats", help="totals across the ledger")
    q_stats.set_defaults(func=cmd_queue_stats)

    p_campaigns = sub.add_parser("campaigns", help="find campaigns with budget left")
    csub = p_campaigns.add_subparsers(dest="campaigns_command", required=True)

    def add_board_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--refresh", action="store_true", help="bypass the cached board")
        p.add_argument(
            "--cache-seconds", type=float, default=3600, help="reuse a board this fresh"
        )
        p.add_argument(
            "--no-record", action="store_true", help="do not add a snapshot to the history"
        )
        p.add_argument("--json", action="store_true")

    c_list = csub.add_parser("list", help="ranked board")
    add_board_flags(c_list)
    c_list.add_argument("--platform", help="only campaigns allowing this platform")
    c_list.add_argument("--min-days", type=float, help="minimum days of runway")
    c_list.add_argument("--min-budget", type=float, help="minimum dollars left")
    c_list.add_argument("--min-cpm", type=float, help="minimum rate per 1k views")
    c_list.add_argument("--max-creators", type=int, help="maximum clippers already competing")
    c_list.add_argument("--no-application", action="store_true", help="skip gated campaigns")
    c_list.add_argument("--query", help="substring of the title, brand or category")
    c_list.add_argument("--limit", type=int, default=20)
    c_list.set_defaults(func=cmd_campaigns_list)

    c_show = csub.add_parser("show", help="one campaign in full, including its brief")
    add_board_flags(c_show)
    c_show.add_argument("campaign", help="campaign ID, ID prefix, or title substring")
    c_show.set_defaults(func=cmd_campaigns_show)

    c_watch = csub.add_parser("watch", help="alert when this campaign runs low")
    add_board_flags(c_watch)
    c_watch.add_argument("campaign")
    c_watch.set_defaults(func=cmd_campaigns_watch)

    c_unwatch = csub.add_parser("unwatch", help="stop watching a campaign")
    c_unwatch.add_argument("campaign")
    c_unwatch.set_defaults(func=cmd_campaigns_unwatch)

    c_check = csub.add_parser(
        "check", help="exit non-zero if a watched campaign is running out (for cron)"
    )
    add_board_flags(c_check)
    c_check.add_argument("--min-days", type=float, default=7.0)
    c_check.set_defaults(func=cmd_campaigns_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except BriefError as exc:
        print(f"brief error: {exc}", file=sys.stderr)
        return 2
    except (BoardError, BoardParseError) as exc:
        print(f"campaign board: {exc}", file=sys.stderr)
        return 4
    except ToolMissing as exc:
        print(f"missing tool: {exc}\nRun `clipper doctor` for the full list.", file=sys.stderr)
        return 3
    except (CommandFailed, KeyError, FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
