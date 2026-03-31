FROM python:3.11-slim

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY pyproject.toml .
RUN uv sync --no-dev

COPY bot.py scraper.py ai.py ./

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
