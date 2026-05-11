# Multi-stage build for smaller image

FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
RUN pip install --user --no-cache-dir hatchling
COPY src ./src
RUN pip install --user --no-cache-dir .

# ── Runtime stage ────────────────────────────────────────────────

FROM python:3.12-slim AS runtime

WORKDIR /app

# Runtime deps only (no -dev variants); chromium libs for playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
        libcurl4 \
        libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libdbus-1-3 libxcb1 libxkbcommon0 libx11-6 \
        libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 \
        libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
        fonts-liberation fonts-unifont ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN useradd -m -r -u 1000 botuser && \
    mkdir -p /data /home/botuser/.cache && \
    chown -R botuser:botuser /data /home/botuser /app

COPY --from=builder --chown=botuser:botuser /root/.local /home/botuser/.local
ENV PATH=/home/botuser/.local/bin:$PATH

# Install playwright chromium as botuser
USER botuser
RUN python -m playwright install chromium 2>/dev/null || true

WORKDIR /app
COPY --chown=botuser:botuser src ./src
COPY --chown=botuser:botuser pyproject.toml ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

VOLUME ["/data"]

CMD ["python", "-m", "price_tracker.main"]
