"""clipper - a repeatable pipeline for cutting short-form clips from long-form source video.

The pipeline is five idempotent stages, each writing its output into the work
directory so a rerun only redoes what changed:

    fetch -> transcribe -> score -> render -> queue

Posting is deliberately NOT a stage. See README.md.
"""

__version__ = "0.1.0"
