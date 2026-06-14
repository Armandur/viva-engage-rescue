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
  config.py    Läser YAMMER_TOKEN / YAMMER_API_BASE från .env, auth_headers()
  yammer.py    API-klient: throttle, 429-backoff, paginering, fil-nedladdning
  dump.py      Råd-dump av alla communities -> data/raw/ (resume via .done/.cursor)
  threads.py   Backfill av hela trådar via in_thread -> data/raw/threads/ (resume)
  download.py  Laddar ner filbilagor från dumpen -> data/attachments/
  graphql.py   Klient mot moderna Engage-GraphQL (persisted queries, self-heal)
  enrich.py    Berikning: reaktioner + seenByCount per tråd -> data/raw/reactions/
  community_info.py  Info-text/pins/medlemmar per community -> data/raw/community_info/
  users.py     Full användarprofil (users.json, hela nätverket) -> data/raw/users/
               + avatarer för arkivdeltagare -> data/avatars/ (resume + progress)
  pipeline.py  Kör hela kedjan i serie (dump->threads->build->storylines->build->
               enrich->community_info->users->download->build) som delprocesser.
               Resumebart, self-heal-token, SIGTERM dödar aktivt delsteg.
  spike.py     Auth-/funktionstest mot API:t
app/
  main.py      Admin/kontrollpanel (FastAPI): startar dump/download, token-byte,
               progress. Inkluderar även arkiv-routern för admins egen bläddring.
  public.py    Publik, läs-bar app: serverar ENBART arkivet (/arkiv), ingen
               styrning. Separat port - det är denna som publiceras externt.
  archive.py   Arkiv-routern (/arkiv) - fristående APIRouter, inkluderas av båda.
  templates/index.html  (panelen)  templates/archive/  (arkiv-UI)
data/          (gitignored) raw/ (rå JSON), attachments/, *.log, run.pid
               SQLite + sök-app byggs härnäst från raw/
```

## Körning

- Admin-panel (intern): `uvicorn app.main:app --host 0.0.0.0 --port 8050`
  (8000-8699 lediga på VMen per 2026-06-09). Styr dump/nedladdning, token, status.
- Publikt arkiv (separat port, det som publiceras externt):
  `uvicorn app.public:app --host 0.0.0.0 --port 8051` - serverar bara /arkiv,
  ingen styrning. Åtkomstskydd sätts i reverse proxyn framför (Nginx Proxy
  Manager). Admin-panelen ska INTE exponeras externt.
- Docker/drift: `Dockerfile` + `docker-compose.yml` (single-container, image från
  `ghcr.io/armandur/viva-engage-rescue`, kör `app.public`; admin bakom compose-
  profil "admin"), `docker-compose.dev.yml` (lokal bygg + reload), GitHub Actions
  i `.github/workflows/docker.yml`. Detaljer i `DOCKER.md`.
- Resume: `dump.py` hoppar grupper med `.done`, återupptar halvklara via
  `.cursor` (older_than). Dör token mitt i grupp tappas inget arbete.
- Bilagor: `download_url` på image/file/video går via Yammer-API:t (proxar
  även SharePoint-lagrade filer) och funkar med samma token.

## Designbeslut

- **Legacy-API, inte Graph:** Graph saknar export av meddelandehistorik per
  2026. Legacy-API:t (`/api/v1`) ger full tråd-/meddelandestruktur.
- **Fångad sessionstoken, inte Entra-app:** användaren är vanlig medlem utan
  garanterad rätt att registrera Entra-appar. Token fångas från webbsession,
  är kortlivad -> kör skrapet i ett svep. Delegated åtkomst = bara det
  användaren själv ser (matchar de aktiva communityn).
- **Rate limit:** legacy-API:t är grovt 10 req/10s. Spiken bara läser; den
  riktiga skrapern ska throttla.
- **Rendering från `rich` via allowlist-sanerare:** meddelandetexten renderas ur
  body.rich (Yammers redan serverside-sanerade HTML). `_RichSanitizer` +
  `render_body()` i `app/archive.py` (stdlib `HTMLParser`, ingen dependency)
  behåller formatering (br, i, strong, b, em, p, hr), escapar all text, skriver
  om yammer-object-spans (`data-yammer-object='TYPE:ID'`) till interna länkar
  (user -> /arkiv/u/{id}, group -> /arkiv/c/{id}, tag -> #namn) och behåller
  externa `<a>` med fail-closed http(s)-href. Valdes över `parsed` eftersom
  parsed plattar radbrytningar och tappar fet/kursiv/stycken. `body_plain`
  driver FTS5-indexet; `body_parsed`/`body_urls` (+ tabellen `tags`) lagras som
  data men ligger inte på renderingsvägen.

## Miljövariabler

- `YAMMER_TOKEN` - bearer-token utan "Bearer "-prefix.
- `YAMMER_API_BASE` - default `https://www.yammer.com/api/v1`.

## Säkerhet

Jobbdata. Token + nedladdat innehåll endast lokalt på VMen, gitignorat.
