FROM python:3.12-slim

# System deps: git for ingestion (clone/fetch), curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency metadata first for cache-friendly layer (README needed for setuptools)
COPY pyproject.toml README.md ./
COPY src/ src/
# Install without cache, no secrets in layer
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# Create non-root user and app data directory
RUN useradd -m appuser \
    && mkdir -p /app/data /home/appuser/.chronotrace \
    && chown -R appuser:appuser /app /home/appuser/.chronotrace

USER appuser

EXPOSE 8000 8001

# Default is agent-api; mcp-server overrides command in compose
CMD ["uvicorn", "chronotrace.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
