PY ?= python3
WORK ?= work
BRIEF ?= campaigns/example/campaign.yml

.PHONY: help install dev test lint doctor init run clean image

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

install:  ## install the CLI and runtime deps
	$(PY) -m pip install -e .

dev:  ## install with whisper + test deps
	$(PY) -m pip install -e '.[whisper,dev]'

test:  ## run the unit tests (no ffmpeg or network needed)
	$(PY) -m pytest

doctor:  ## check external tools
	$(PY) -m clipper.cli doctor

init:  ## scaffold campaigns/example/campaign.yml
	$(PY) -m clipper.cli init campaigns/example

campaigns:  ## rank open campaigns by remaining budget
	$(PY) -m clipper.cli --work $(WORK) campaigns list

check:  ## exit non-zero if a watched campaign is running out (for cron)
	$(PY) -m clipper.cli --work $(WORK) campaigns check --min-days 7

run:  ## run the full pipeline for BRIEF
	$(PY) -m clipper.cli --work $(WORK) run --brief $(BRIEF)

image:  ## build the container image (ffmpeg + whisper included)
	docker build -t clipper:latest .

clean:  ## drop rendered clips and caches, keep the ledger
	rm -rf $(WORK)/clips $(WORK)/sources
