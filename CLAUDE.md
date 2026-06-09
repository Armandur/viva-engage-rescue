# CLAUDE.md - viva-engage-rescue

## Vad projektet är

Skrapar Viva Engage-communities (jobbets nätverk) innan migrering och bygger
ett sökbart SQLite-arkiv. Engångsjobb i grunden, men arkiv-appen lever kvar.

## Stack

- Python 3.12, `uv` för beroenden (`uv run python -m ...`).
- `requests` mot legacy Yammer REST API (`https://www.yammer.com/api/v1`).
- Lagring: SQLite + FTS5 (planerad). Råa API-svar sparas också i `data/raw/`.
- Arkiv-app (planerad): FastAPI + Jinja2, vanilla JS.

## Filstruktur

```
scraper/
  config.py   Läser YAMMER_TOKEN / YAMMER_API_BASE från .env, auth_headers()
  spike.py    Auth-/funktionstest mot API:t (users/current, groups, messages)
app/          (kommer) FastAPI-arkiv med sök
data/         (gitignored) SQLite + raw/
```

## Designbeslut

- **Legacy-API, inte Graph:** Graph saknar export av meddelandehistorik per
  2026. Legacy-API:t (`/api/v1`) ger full tråd-/meddelandestruktur.
- **Fångad sessionstoken, inte Entra-app:** användaren är vanlig medlem utan
  garanterad rätt att registrera Entra-appar. Token fångas från webbsession,
  är kortlivad -> kör skrapet i ett svep. Delegated åtkomst = bara det
  användaren själv ser (matchar de aktiva communityn).
- **Rate limit:** legacy-API:t är grovt 10 req/10s. Spiken bara läser; den
  riktiga skrapern ska throttla.

## Miljövariabler

- `YAMMER_TOKEN` - bearer-token utan "Bearer "-prefix.
- `YAMMER_API_BASE` - default `https://www.yammer.com/api/v1`.

## Säkerhet

Jobbdata. Token + nedladdat innehåll endast lokalt på VMen, gitignorat.
