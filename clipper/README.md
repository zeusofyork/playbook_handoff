# clipper

A repeatable pipeline for cutting short-form clips out of long-form source
video: fetch, transcribe, rank the moments worth using, reframe to 9:16, burn
in captions, and stage the result with a caption and a checklist.

Posting is not a stage. See [Where the automation stops](#where-the-automation-stops).

## Why this exists

Most clipping is done by hand in CapCut, one clip at a time, which caps output
at whatever you can personally sit through. This turns the repeatable 90% into
a pipeline and leaves the judgement calls — is this actually funny, does the
hook land, is it worth posting — to you.

The economics are worth stating plainly before you build a workflow around it.
Campaigns advertise roughly $0.20–$6 per 1,000 views, but the blended rate
actually paid across all tracked views tends to land near $0.39 per 1,000.
`clipper queue stats` prints your own `effective_cpm_usd` against the headline
rate in the brief, so you find out which one you're living in.

## Install

```bash
pip install -e '.[whisper]'     # or: make dev
clipper doctor                  # check ffmpeg, ffprobe, yt-dlp, whisper
```

You need `ffmpeg` (with `ffprobe`) on PATH. `yt-dlp` and `faster-whisper` come
from the package; whisper is optional — without it, use `--no-transcript` and
selection falls back to audio energy alone, with no captions.

Prefer a container:

```bash
make image
docker run --rm -v "$PWD/work:/work" -v "$PWD/models:/models" \
  -v "$PWD/campaigns:/campaigns" clipper:latest run --brief /campaigns/foo/campaign.yml
```

## Quickstart

```bash
clipper init campaigns/foo          # writes a commented campaign.yml
$EDITOR campaigns/foo/campaign.yml  # fill in from the campaign's own brief
clipper run --brief campaigns/foo/campaign.yml
clipper queue list
```

Then watch each clip, post the ones worth posting by hand, and record them:

```bash
clipper queue posted a1b2c3 --platform tiktok --url https://...
clipper queue views  a1b2c3 --platform tiktok --views 41000 --brief campaigns/foo/campaign.yml
clipper queue stats
```

Clip IDs can be abbreviated to any unique prefix.

## Finding campaigns worth clipping

Headline CPM is the wrong thing to sort on. Budget is pooled per campaign, not
per clipper, so a pool that runs dry in two days is worth nothing whatever its
rate. `clipper campaigns` pulls the public discover board, works out how fast
each pool is draining, and ranks by what is actually left for you:

```bash
clipper campaigns list --platform tiktok --min-days 14 --no-application
```

```
ID           CPM     LEFT   BURN/D  RUNWAY  CLIPPERS   $/CLIP  SCORE  CAMPAIGN
efa2656a   $5.00    $2.2k      $33     67d        25      $88  0.814    Maxim Hair Restoration Campaign
ecbd7fec   $1.00   $39.0k     $446     87d       305     $128  0.757    Lovable Clipping
334ba2c6   $3.00    $6.7k     $226     30d       117      $57  0.714    Syberjet Speed Record
1db63081   $1.00    $8.2k    $4.1k      2d      2941       $3  0.202    ForgeGUI Clipping [Roblox]
```

That last row is the point: $8.2k left at $1 CPM looks healthy in a table of
budgets, and it is gone in two days.

### Burn rate

Two estimates, in order of preference:

- **observed** — the drop in available budget between two of your own runs.
  Recent and real, but needs at least two snapshots an hour or more apart.
- **lifetime** — spend since funding divided by campaign age. Available on the
  very first run, but it is an average, so it understates a pool that only just
  got busy.

Every run records a snapshot, so the estimate improves on its own. Run it from
cron and you build the series for free.

### Ranking

`score` combines three components, each normalised across the board:

| Component | Weight | What it measures |
|---|---|---|
| `runway` | 0.45 | days of budget left, flattening out at 30 |
| `rate` | 0.25 | CPM relative to the best on the board |
| `headroom` | 0.30 | dollars left per clipper already competing, as a percentile |

Runway is weighted highest on purpose: a dead pool pays nothing at any rate.
It is a heuristic for ordering a shortlist, not a forecast — `campaigns show`
prints the components so you can disagree with the weighting.

### Watching a campaign

```bash
clipper campaigns watch ecbd7fec
clipper campaigns check --min-days 7     # exits 1 if a watched pool is running out
```

`check` is built for cron. Non-zero exit means a pool you are actively clipping
is about to stop paying, or has dropped off the board entirely:

```cron
0 */6 * * * cd ~/clipping && clipper campaigns check --min-days 7 || notify-send "clipping budget low"
```

Only the public `/discover` page is fetched, never the `/api/` paths the site's
robots.txt disallows, and responses are cached for an hour by default
(`--refresh` to force). If the board's markup changes, parsing fails loudly with
a non-zero exit rather than quietly reporting an empty board.

## The brief is the spec

`campaign.yml` encodes the campaign's rules — duration bounds, aspect, required
attribution, banned words, payout terms. Every stage reads it, and `render`
refuses to encode a clip that would break it:

```
WARNING clipper.render: skipped 0:14:02-0:14:48 score=0.71: duration 46.0s is
over max_seconds 45; banned word(s) present: giveaway
```

Failing there costs a second. Failing at submission costs the clip.

Unknown keys are rejected rather than ignored, so a typo in a threshold is a
startup error instead of a knob that silently does nothing:

```
brief error: invalid brief:
  - unknown platforms: myspace
  - width and height must both be even (h264 yuv420p requirement)
```

## Stages

Each stage caches into the work directory and is safe to re-run. `clipper run`
chains all of them.

| Stage | Command | Output |
|---|---|---|
| fetch | `clipper fetch` | `work/sources/<id>/source.mp4` + `source.json` |
| transcribe | `clipper transcribe` | `transcript.json` (word-level timings) |
| score | `clipper score` | `moments.json` (ranked, non-overlapping) |
| render | `clipper render` | `work/clips/<id>/clip.mp4`, `captions.ass`, `caption.txt`, `POST.md` |
| queue | `clipper queue ...` | `work/queue/ledger.json` |

Campaign discovery (`clipper campaigns`) sits outside that chain and caches into
`work/campaigns/`.

A local file path works anywhere a URL does, and nothing derived from it is
written next to your original media.

### How moments get picked

Four signals are resampled onto one 0.5s timeline and combined with the weights
from the brief:

- **energy** — loudness of the source audio (shouting, laughter, a music swell)
- **hook_energy** — loudness of the window's *first three seconds*, because a
  clip that opens flat gets scrolled past no matter how good it gets later
- **speech_density** — words per second, so dead air ranks low
- **keyword** — campaign keywords spoken inside the window

Candidate windows slide across the source, get scored, then go through
non-maximum suppression so you get N moments spread across the video instead of
N off-by-one views of the same ten seconds. Boundaries are then snapped onto
nearby word edges so clips don't open mid-syllable.

Tune it by re-running `clipper score` and reading the ranked output before you
spend encode time:

```bash
clipper score --brief campaigns/foo/campaign.yml
 1. 0:04:12-0:04:46 score=0.812  so we deleted the entire cluster by accident
 2. 0:11:03-0:11:38 score=0.774  nobody reads the terraform plan, be honest
```

Changing any selection knob invalidates the cached ranking automatically — the
brief's fingerprint is stored alongside the moments.

### Reframing

- `crop` — fill the frame, cutting the sides off a 16:9 source (`crop_anchor`
  picks which side survives)
- `blur_pad` — fit the whole frame over a blurred, darkened copy of itself
- `none` — letterbox onto the canvas, never crop

### Captions

Word timings from the transcript are rebased onto the clip's timeline and
chunked into short cues bounded by word count, line length and duration, then
burned in with ffmpeg. Raise `caption_style.margin_v` if the platform's own UI
covers them. Most of these clips are watched with the sound off; the captions
are not optional.

## Where the automation stops

**Posting stays manual.** Driving TikTok or Instagram with a browser bot breaks
their terms and gets accounts banned, which ends the whole operation — the
throughput you gain is not worth the account you lose. If you want posting
automated, use the platforms' official publishing APIs under your own developer
credentials, and keep the approval step.

**Only clip what you are licensed to clip.** That means the campaign's supplied
content, content whose brief explicitly grants clipping rights, or your own
footage. Clipping someone's stream without that is a copyright strike, not a
side hustle.

**The payout figures are estimates.** `economics` in the brief drives
`queue stats`; it reflects what a campaign advertises, not what it pays.

## Work directory layout

```
work/
├── sources/<source_id>/
│   ├── source.mp4          # or a pointer to your local file
│   ├── source.json         # probed metadata
│   ├── transcript.json     # word-level timings
│   └── moments.json        # ranked selection + brief fingerprint
├── clips/<clip_id>/
│   ├── clip.mp4            # the finished vertical clip
│   ├── captions.ass        # what got burned in
│   ├── caption.txt         # the post caption
│   ├── meta.json           # provenance: source, timestamps, score, components
│   └── POST.md             # pre-publish checklist
├── queue/ledger.json       # what you posted, where, and what it earned
└── campaigns/
    ├── board.html          # cached discover page
    ├── history.json        # budget snapshots, for observed burn rate
    └── watch.json          # campaigns to alert on
```

`meta.json` keeps every clip traceable back to its source and timestamps, which
is what you want when a campaign disputes a submission.

## Development

```bash
make test     # 140+ unit tests, no ffmpeg or network needed
make doctor
```

The ffmpeg-dependent tests skip themselves when it isn't installed. The scoring
math, caption chunking, brief validation, filter-graph construction, board
parsing, burn-rate estimation and ledger accounting are all pure functions and
tested directly; the board parser runs against a committed fixture, so no test
touches the network.
