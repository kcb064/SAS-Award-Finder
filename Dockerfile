# EuroBonus Award Finder image.
#
# The Phase 1 spike proved the SAS award-finder feed is fetchable only through REAL Google Chrome
# (bundled Chromium / headless-shell are Cloudflare-403'd). So we start from Playwright's Python
# base image (which brings the matching browser deps) and additionally install branded Google
# Chrome stable, launched via channel="chrome" at runtime.
#
# IMPORTANT: the base image tag MUST match the pinned `playwright` version in pyproject.toml.
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AF_DATA_DIR=/data \
    AF_CONFIG_DIR=/config \
    AF_PORT=8617 \
    AF_HEADLESS=true \
    AF_BROWSER_CHANNEL=chrome

WORKDIR /app

# Install Python deps first (better layer caching). Copy only what pip needs to resolve.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

# Install branded Google Chrome stable (NOT bundled Chromium) for channel="chrome".
RUN python -m playwright install --with-deps chrome

# Default config baked into the image; the entrypoint seeds a mounted /config from it on first run.
COPY config ./config-default
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh && mkdir -p /config /data

EXPOSE 8617

# Single process: uvicorn serves the app; APScheduler runs in its event loop.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8617"]
