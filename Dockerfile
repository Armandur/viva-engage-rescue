# Single-container för viva-engage-rescue. Kör som standard det publika arkivet
# (app.public). Admin-panelen (app.main) kan startas i samma image genom att
# överrida CMD (se docker-compose.yml: profil "admin").
FROM python:3.12-slim

# uv från den officiella imagen (snabb, lockfil-baserad install).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

# Beroenden i ett eget cache-lager (ändras sällan).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Applikationskoden.
COPY app ./app
COPY scraper ./scraper

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8051

# data/ monteras som volym i drift (archive.db, attachments, avatars, thumbnails).
CMD ["uvicorn", "app.public:app", "--host", "0.0.0.0", "--port", "8051"]
