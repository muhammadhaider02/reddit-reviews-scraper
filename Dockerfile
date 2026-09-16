FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # Browser binaries live at a fixed path so the non-root runtime user finds what root installed.
    XDG_CACHE_HOME=/opt/cache \
    PLAYWRIGHT_BROWSERS_PATH=/opt/cache/ms-playwright

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Installs the stealth browser plus its system libraries. Needs root, hence before USER.
RUN uv run --no-sync scrapling install

RUN useradd --create-home scraper && chown -R scraper:scraper /app /opt/cache
USER scraper

EXPOSE 8001
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/health').status==200 else 1)"
CMD ["uv", "run", "--no-sync", "reddit-reviews", "serve"]
