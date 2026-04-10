FROM python:3.11-slim

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

COPY bot.py scraper.py ai.py personalization.py scheduler.py agent.py db.py models.py ./
COPY config/ config/
COPY migrations/ migrations/
COPY alembic.ini ./

CMD ["uv", "run", "bot.py"]
