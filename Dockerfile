# Multi-stage build for smaller image

FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Dependencies come from uv.lock, not from a fresh resolution. Before this, the
# build copied pyproject.toml alone and ran `pip install .`, so every rebuild
# resolved the transitive tree afresh: the image was not reproducible, and the
# lockfile dependabot maintains never reached the runtime at all — anyio and
# soupsieve, both bumped to patched versions in the lock, are transitive through
# httpx and beautifulsoup4 and are not named in pyproject.toml.
#
# `uv export` is run rather than committing a generated requirements file: a
# second copy of the pinned tree would drift from the lock, and nothing would
# say so. uv itself is installed without --user so it stays out of /root/.local,
# which the runtime stage copies wholesale.
COPY pyproject.toml uv.lock README.md ./
RUN pip install --user --no-cache-dir hatchling \
    && pip install --no-cache-dir uv==0.12.7
COPY src ./src
RUN uv export --frozen --no-dev --no-emit-project --format requirements.txt \
        -o /tmp/requirements.txt \
    && pip install --user --no-cache-dir --no-deps --require-hashes \
        -r /tmp/requirements.txt \
    && pip install --user --no-cache-dir --no-deps .

# ── Runtime stage ────────────────────────────────────────────────

FROM python:3.12-slim AS runtime

WORKDIR /app

# Runtime deps only (no -dev variants). No browser: rendering is disabled
# because Chromium cannot use the validated-address transport, so neither
# Chromium nor its system libraries and fonts are installed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN useradd -m -r -u 1000 botuser && \
    mkdir -p /data /home/botuser/.cache && \
    chown -R botuser:botuser /data /home/botuser /app

COPY --from=builder --chown=botuser:botuser /root/.local /home/botuser/.local
ENV PATH=/home/botuser/.local/bin:$PATH

USER botuser

WORKDIR /app
COPY --chown=botuser:botuser src ./src
COPY --chown=botuser:botuser pyproject.toml ./

# MPLCONFIGDIR points at the writable cache: with read_only rootfs matplotlib
# cannot create its default directory under $HOME/.config, so it rebuilds a
# throwaway cache — and logs a warning — on every chart render.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    MPLCONFIGDIR=/home/botuser/.cache/matplotlib

VOLUME ["/data"]

CMD ["python", "-m", "price_tracker.main"]
