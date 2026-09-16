FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # Browser binaries live at a fixed path so the non-root runtime user finds what root installed.
    XDG_CACHE_HOME=/opt/cache \
    PLAYWRIGHT_BROWSERS_PATH=/opt/cache/ms-playwright

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app
# Dependencies and the browser resolve from the lockfile alone, so they cache independently of
# application code. Ordering src after them keeps a one-line change off the multi-minute Chromium
# layer; with src copied first, every code edit re-downloaded the browser.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Installs the stealth browser plus its system libraries. Needs root, hence before USER.
RUN uv run --no-sync scrapling install

# The recursive chown is a 1.45GB layer (it covers the browser cache), so it has to sit above
# the source copy. Below it, a code change would rewrite all 1.45GB on every build.
RUN useradd --create-home scraper && chown -R scraper:scraper /app /opt/cache

# Everything below is invalidated by a code change, so keep it small: ~135kB of source and the
# ~172kB of installing the project itself into the already-built venv.
COPY --chown=scraper:scraper src ./src
RUN uv sync --frozen --no-dev

USER scraper

EXPOSE 8001
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/health').status==200 else 1)"
CMD ["uv", "run", "--no-sync", "reddit-reviews", "serve"]
