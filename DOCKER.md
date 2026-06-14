# Docker / drift

Containeriserad enligt standardstacken: single-container, image till GHCR,
`docker-compose.yml` för Unraid-drift, `docker-compose.dev.yml` för lokal bygg.

## Vad som körs

Imagen innehåller hela appen. Som standard kör containern **det publika arkivet**
(`app.public`, läs-bart `/arkiv`) på port **8051**. Admin/kontrollpanelen
(`app.main`, skrapning/token/status) körs bara vid behov och exponeras aldrig
externt.

## Drift på Unraid (TERVO2)

1. Image byggs och publiceras automatiskt av GitHub Actions till
   `ghcr.io/armandur/viva-engage-rescue:latest` vid push till `main`.
2. På Unraid: kör `docker-compose.yml`. Peka volymen `/app/data` mot en host-path,
   t.ex. `/mnt/user/appdata/viva-arkiv/data`.
3. Sätt åtkomstskydd (basic auth) i **Nginx Proxy Manager** framför port 8051.
   Admin-panelen ska inte ligga bakom NPM.

```bash
docker compose up -d            # publika arkivet (8051)
docker compose --profile admin up -d admin   # admin (8050), bara vid omkörningar
```

Det publika arkivet monterar `data/` read-only. Admin-profilen monterar rw
(skriver vid skrapning/bygge) och behöver `.env` med `YAMMER_TOKEN`.

## Importera redan hämtad data

All insamlad data ligger i `data/` på dev-VM:en (rådata, `archive.db`,
`attachments/` ~28 GB, `avatars/`, `thumbnails/`). Importera till Unraid:

```bash
# från dev-VM:en till Unraid-volymen (justera sökväg)
rsync -av --progress data/ TERVO2:/mnt/user/appdata/viva-arkiv/data/
```

Gör detta **efter den sista omkörningen** (panelens "Kör hela kedjan") så den
mest kompletta datan importeras. Endast `data/` behöver flyttas - allt UI bygger
på `archive.db` + de monterade filmapparna.

## Lokal utveckling

```bash
docker compose -f docker-compose.dev.yml up --build   # bygger lokalt, --reload
```

Monterar `app/` och `scraper/` så ändringar slår igenom direkt.

## Bygga/testa imagen för hand

```bash
docker build -t viva-engage-rescue:test .
docker run --rm -p 8052:8051 -v "$PWD/data:/app/data:ro" viva-engage-rescue:test
# -> http://ubuntu-ai:8052/arkiv
```
