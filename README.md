# viva-engage-rescue

Skrapar jobbets Viva Engage-communities innan nuvarande instans stängs ner
för molnmigrering, och lagrar allt i ett sökbart SQLite-arkiv.

## Bakgrund

Viva Engage-nätverket ska migreras från ett amerikanskt moln. Det är oklart
om all historik följer med, så detta verktyg gör en egen fullständig kopia av
de communities man har åtkomst till.

- **Datakälla:** Legacy Viva Engage (Yammer) REST API
  (`https://www.yammer.com/api/v1`). Microsoft Graphs Viva Engage-API kan
  lista communities men saknar (per 2026) export av meddelandehistorik, så
  legacy-API:t är vägen för innehållet.
- **Auth:** Den gamla Yammer-token-plattformen pensionerades 2025-06-30.
  Anrop sker numera med en bearer-token från en Entra-app *eller* en token
  fångad från en inloggad webbsession. Detta projekt använder det senare:
  delegated åtkomst = exakt det du själv ser, vilket matchar "de aktiva
  communityn".

## Status

Tidigt skede. Just nu finns en **auth-spike** som verifierar att en fångad
token funkar mot API:t. Skraper och arkiv-app byggs när spiken bekräftats.

## Kom igång

```bash
cd ~/workspace/viva-engage-rescue
cp .env.example .env
# Fånga din token: logga in på Viva Engage i webbläsaren, öppna DevTools ->
# Network, hitta ett anrop mot www.yammer.com/api/..., kopiera värdet efter
# "Authorization: Bearer " och klistra in som YAMMER_TOKEN i .env.

uv run python -m scraper.spike
```

Förväntad output: vem du är inloggad som, ditt nätverk, en lista över dina
communities och första sidan meddelanden i den första.

## Token-sync (Tampermonkey)

Device code-inloggning är blockerad av Conditional Access, så token måste komma
från en webbläsare på en godkänd enhet. `browser/viva-token-sync.user.js` är ett
userscript som fångar din aktiva bearer-token och postar den till panelen
(`/api/token`) så fort webbläsaren förnyar den (~var 75:e min medan en Viva-flik
är öppen). Då hålls dumpen igång utan manuell inklistring.

1. Installera Tampermonkey, lägg till scriptet.
2. Justera `PANEL`-konstanten om panelen inte kör på `http://ubuntu-ai:8050`.
3. Öppna Viva Engage - en grön ruta nere till höger bekräftar att token skickats.

## Planerad struktur

```
scraper/        Hämtning från legacy-API:t
  config.py     Läser token från .env
  spike.py      Auth-/funktionstest (finns nu)
app/            Liten FastAPI + Jinja2-app för att bläddra och söka (FTS5)
data/           SQLite-arkiv + raw/ med oförändrade API-svar (gitignored)
```

## Säkerhet

Jobbdata. Token och nedladdat innehåll ligger bara lokalt på utvecklings-VMen
och är gitignorade. Inget skickas till externa tjänster.
