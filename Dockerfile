FROM python:3.11-slim

WORKDIR /app

RUN pip install uv --no-cache-dir

# Layer 1: dependencies only (cached unless pyproject/lock change)
COPY pyproject.toml uv.lock ./
RUN uv sync --no-install-project --no-dev --frozen

# Layer 2: the package + runtime config, then install the project itself
COPY src/ src/
COPY config/ config/
RUN uv sync --no-dev --frozen

CMD ["uv", "run", "python", "-m", "digest_bot.bot"]
