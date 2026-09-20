# ffmpeg and the whisper runtime are the awkward dependencies; pin them in an
# image so a render box is reproducible and CI does not have to solve them.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[whisper]'

# Whisper models land here; mount it as a volume so they survive container churn.
ENV HF_HOME=/models XDG_CACHE_HOME=/models
VOLUME ["/work", "/models"]

ENTRYPOINT ["clipper", "--work", "/work"]
CMD ["doctor"]
