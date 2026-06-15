# Skiss: verktyg för att importera arkivet till en plattform

Designskiss (inte byggt). Hur ett verktyg som skriver in de arkiverade inläggen -
valda communities eller alla - i en målplattform skulle kunna se ut, med färdiga
alternativ och en egen importer via API:erna.

> **Uppdaterad 2026-06-15:** skriv-API:t är nu dokumenterat mot Microsoft Learn (se
> §A) och en PoC-importer är byggd (`scraper/importer.py`, se §B). Skissen kör
> fortfarande inga skrivningar förrän ett **smoke-test** godkänns - att posta mot ett
> levande nätverk är en utåtriktad åtgärd (och token-ens skrivbehörighet är ännu
> overifierad).

---

## 0. Frågan som måste besvaras först: vart, och varför?

Två saker avgör allt nedan och bör beslutas innan något byggs:

**a) Vilken målplattform?** "Importera tillbaka till Viva Engage" är troligen
poänglöst - Viva stängs ju ner. De enda rimliga målen är:
- en **efterträdare / ny tenant** (nytt Viva-nätverk, Teams, SharePoint, en wiki...),
- eller **ingen** - vi har redan byggt det durabla: det läs-bara SQLite-arkivet
  bevarar allt (innehåll, trådar, reaktioner, seen, bästa svar, bilagor, profiler)
  med **full trohet** och utan beroende av en levande plattform.

**b) Varför re-import?** En re-import **tappar trohet**: författarskap, äkta
tidsstämplar och reaktioner/seen/bästa-svar kan inte återskapas som riktiga objekt
(se §4) utan degraderas till text. Den är dessutom **tung och irreversibel** (flödes-
spam, notiser, skriv-rate-limits, timmar-till-dygn). Arkivet vi har är redan
"bevarandet". Re-import är motiverat bara om innehållet måste *leva i en
målplattform* (sökbart/kommenterbart där användarna är), inte bara bevaras.

Resten av skissen antar att svaret blev "ja, importera till en Viva-liknande
plattform vars API liknar det vi dokumenterat". Är målet ett helt annat system
(SharePoint/Teams/wiki) ändras API-detaljerna men arkitekturen (§3-§6) håller.

---

## 1. Färdiga verktyg?

Kort: **inget bra färdigt alternativ för det här fallet.**
- Microsoft erbjuder **export** (compliance/eDiscovery) men ingen *import*-väg som
  återskapar communities + trådar med innehåll.
- Tredjeparts **tenant-till-tenant-migrering** för Yammer/Viva finns (enterprise,
  betald) men är gjord för att flytta ett *levande* nätverk mellan tenants med
  bevarad identitet - inte för att skriva in ett externt arkiv som ett "arkiv-konto".
  Passar illa och löser inte trohetsförlusten ovan.

Slutsats: ska innehållet in i en plattform blir det i praktiken ett **eget verktyg**.
(Obs: jag kan inte surfa och verifiera aktuellt utbud - detta bör dubbelkollas.)

---

## 2. Egen importer - grundidé

Som du skissade:
1. Ett **arkiv-konto** (service-konto, medlem/admin i målnätverket).
2. Det skapar en **arkiv-community per ursprunglig community** (t.ex.
   "[Arkiv] Kontaktperson IT i Svenska kyrkan"), valbart för alla eller utvalda.
3. Det **postar in trådarna** - trådstart som nytt inlägg, svar som svar - i
   kronologisk ordning per community.

Volym att ta höjd för (ur vårt arkiv): **98 communities, ~8 900 trådar,
~30 300 meddelanden**, plus bilagor.

---

## 3. Arkitektur

```
Källa: archive.db (vårt arkiv)
   |
   v
Importer (idempotent, resumebar)
   1. skapa arkiv-community per vald grupp        -> map  old_group_id -> new_group_id
   2. för varje tråd (sorterad på trådstart-tid):
        skapa trådstart  -> map old_msg_id -> {new_msg_id, new_url}
        skapa varje svar i kronologisk ordning, med replied_to_id = förälderns NYA id
   3. (om länkfix behövs) andra pass: redigera inläggstexter och byt
        gamla post-länkar mot nya URL:er ur mappen
   |
   v
Mål-plattform (API)   +   id-map på disk (resume + länkfix)
```

Persistent **id-map** (`old_id -> new_id/url`, per community och meddelande) är
navet: den ger idempotens (hoppa redan skapade), resume vid avbrott, och
underlaget för länkåterskapningen.

---

## 4. Tre saker som inte kan återskapas troget -> bäddas in i texten

Med en vanlig (delegerad) token postar arkiv-kontot **som sig självt, med nutid som
tidsstämpel**. Följande går därför sannolikt inte att sätta via API:t och måste in
i brödtexten i stället (en eventuell admin-/migrerings-API *kan* skilja sig - det är
**overifierat**, påstå inte att det är omöjligt):

- **Författare** -> prefix i inlägget: *"Ursprungligen av Anna Andersson · 2023-09-12"*
  (länka/@-nämn originalförfattaren om hen finns i målnätverket, annars som text).
- **Tidsstämpel** -> visas i prefixet; *ordningen* i flödet löser vi genom att posta
  kronologiskt (§5), så det läser rätt även om de tekniska tiderna är importdatum.
- **Reaktioner / seen / bästa svar** -> sidfot i inlägget, t.ex.
  *"👍 30 · ❤️ 5 · 👁 sett av 142 · ✓ markerat som bästa svar"* (+ ev. lista reaktörer).
  Kan inte återskapas som riktiga reaktioner (kontot kan inte reagera åt andra).

---

## 5. Trådning, ordning och länkåterskapning ("tågordningen")

Här finns **två beroende-ordningar** som kan krocka:
- **Trådordning:** ett svar behöver förälderns *nya* `replied_to_id` -> föräldern
  måste skapas först. (Per tråd en trivial DAG: start -> svar -> nästlade svar.)
- **Länkordning:** ett inlägg som länkar till ett annat arkiverat inlägg behöver
  målets *nya* URL -> målet måste skapas först. Korsar trådar och kan ha cykler
  (A länkar B, B länkar A).

En enda global topologisk sortering som uppfyller båda är ofta **omöjlig** (kors-
trådslänkar + cykler). Därför hänger valet på **en fråga: kan ett inlägg redigeras
efter att det skapats?**

**Alt. A - två-pass (rekommenderas om redigering stöds):**
1. **Pass 1 - skapa allt** i enkel tråd- + kronologisk ordning (parent-före-barn är
   en trivial per-tråd-ordning, inga cykler). Spara `old_id -> new_url` löpande.
2. **Pass 2 - fixa länkar:** redigera bara de inlägg vars text innehåller interna
   post-länkar och byt mot nya URL:er ur den nu kompletta mappen.

Det **sidsteppar** topologisk sortering och cykel-problemet helt. Länkar till
inlägg i *ej importerade* communities kan pekas mot vårt läs-arkiv eller lämnas som
"(arkiverad länk)".

**Alt. B - topologisk ordning (fallback om redigering INTE stöds):**
Bygg en beroendegraf (inlägg -> inlägg det länkar till), topologisk-sortera, och
substituera nya URL:er inline vid skapandet (målen finns redan då). Kvarvarande
cykler/korsande beroenden blir brutna länkar eller platshållare. Det är "tågordningen"
du beskrev - men den är skörare och löser inte cykler.

**Gaffeln att verifiera:** *stödjer mål-API:t redigering av inläggstext?* Ja -> Alt. A.
Nej -> Alt. B.

**AVGJORT (2026-06-15):** Yammer REST har **ingen** redigerings-endpoint (inget
PUT/PATCH, se §A). Alt. A faller alltså. **Men** för en enskild community behövs
varken Alt. B:s graf eller topologisk sortering: kronologisk postning löser alla
*bakåtlänkar* (länkmålet finns redan när vi postar), och allt annat (framåt-/kors-
länkar) pekas mot det **befintliga läs-arkivet** (`http://ubuntu-ai:8051/arkiv/t/<id>`).
Då blir ingen intern länk trasig och vi slipper både andra-pass och topo-sort. Det är
vägen PoC-importern (§B) tar.

## C. Nästling - verifierat 2026-06-15 (Claude-for-Chrome)

Legacy REST `replied_to_id` **plattar** alla svar till trådnivå (observerat i PoC:n).
Den moderna webbklienten skapar nästling via GraphQL-mutationen
**`PublishReplyMessageClients`** (hash i graphql.HASHES). Två variabler styr:
- `replyToMessageMutationId` = base64 `{"_type":"Message","id":"<id>"}` -> pekar på
  **den direkta föräldern** (trådstart ELLER ett annat svar). Back-end bevarar hela
  kedjan, **godtyckligt djup**.
- `isSecondLevelReply` = `false` om föräldern är trådstarten, annars `true`. (Bara två
  lägen - ingen "nivå 3"-flagga; allt djupare än nivå 1 är "second level".)

**Viva-UI:t renderar bara 2 indragsnivåer.** Svar på nivå 3+ läggs visuellt på nivå 2,
och Viva disambiguerar djupet med en inledande **`@Namn`-text** i svaret (inte ett
citat) - exakt det vår text-transform redan gör. Så GraphQL-vägen ger korrekt
föräldralänkning *och* native-likt utseende. body skickas som DraftJS
`serializedContentState` (JSON-sträng, ett block per rad).

Implementerat (scaffolding) i importern: `_post_reply_gql()` + `_content_state()` +
kommandot `smoke-nested` (trådstart via REST, svar via GraphQL, dumpar mutationens
råsvar för att fastställa id-formen). Mutationshashar dör vid app-deploy precis som
läs-hasharna -> nästlad import är ett en-gångs-pass nära fångsten.

---

## 6. @mentions, notiser och bilagor

- **@mentions:** mappa gammal user-id -> mål-user-id om personen finns i målnätverket,
  annars platt text "@Namn (arkiverad)".
- **Notis-/flödesspam:** äkta @mentions och nya inlägg **trigga­r notiser** - 30 000
  inlägg som nämner folk skulle spamma hela organisationen. Rendera mentions som
  **text** (inte riktiga mentions) och/eller posta i en tyst/avnotifierad community,
  annars blåser importen ut aviseringar till alla som någonsin nämnts.
- **Bilagor:** vi har filerna lokalt (`data/attachments/`) - ladda upp via mål-API:ts
  pending-attachment-flöde och koppla till det nya inlägget. Länkkort (ymodule) kan
  återges som vanlig länk + ev. lokal miniatyr.

---

## 7. Drift: idempotens, rate-limits, volym

- **Idempotens/resume:** id-mappen på disk -> hoppa redan skapade communities/inlägg;
  återuppta efter token-utgång (self-heal som i skraparen).
- **Rate-limits:** **skriv**-takten är typiskt strängare än läs. Throttla hårt,
  backa av vid 429.
- **Volym/tid:** ~30 000 skapanden + ett redigerings-pass, vid skriv-rate-limits
  -> **många timmar till dygn**. Kör i bakgrunden, resumebart, gärna community för
  community så man kan börja med de viktigaste.

---

## 8. Måste verifieras innan något byggs (gated - utåtriktade skrivningar)

Hela designen vilar på skriv-förmågor vi **inte testat**. Att testa dem = att skapa
riktiga inlägg i ett levande nätverk, alltså en sidoeffekt som ska godkännas först.
Innan bygge, verifiera (med ett **test-konto / test-community**):
1. Kan token **posta** ett meddelande alls (`POST messages.json`)?
2. Kan man sätta **`replied_to_id`** så svar hamnar i rätt tråd?
3. Kan man **redigera** en inläggstext efteråt? (avgör §5 Alt. A vs B)
4. Går det att **ladda upp bilagor** och koppla dem?
5. Kan författare/tid sättas via någon **admin-/migrerings-väg**? (annars §4)
6. Hur ser **skriv-rate-limit** ut i praktiken?

---

## 9. Risk & rekommendation

- En re-import **degraderar** (författare/tid/reaktioner blir text) och är tung +
  irreversibel. Vårt SQLite-arkiv bevarar redan allt med full trohet.
- **Rekommendation:** behandla det läs-bara arkivet som det primära bevarandet.
  Bygg importern bara om innehållet *måste leva i en målplattform*, och först efter
  att §8 verifierats. Om/när det blir aktuellt: **§5 Alt. A (två-pass)** är den rena
  vägen; arkiv-konto + arkiv-communities + kronologisk postning är rätt grundform.
- Mellanväg värd att överväga: importera bara **utvalda** communities (de som
  verkligen ska leva vidare) och länka allt annat till läs-arkivet - mindre spam,
  mindre trohetsförlust, snabbare.

---

## A. Verifierat skriv-API (Microsoft Learn, 2026-06-15)

Legacy Yammer REST (`https://www.yammer.com/api/v1`), samma token-typ som läs-skrapet:

- **Skapa privat community:** `POST groups.json` med `name`, `private=true`,
  `show_in_directory=false` -> `201` + nytt grupp-id. (Publika *unlisted* grupper är
  ogiltiga; privata unlisted funkar.)
- **Skapa inlägg:** `POST messages.json` (form-encoded). Nyckelparametrar: `body`,
  `group_id` (trådstart i en grupp), `replied_to_id` (svar i befintlig tråd -
  group/network infereras därifrån), `is_rich_text:true` + `message_type:announcement`
  + `title` (meddelande-typ), `og_url`/`og_*` (länkkort), `attached_objects[]:
  uploaded_file:<id>` (bilaga, filen först via uploadSmallFile, ≤4 MB). Svaret
  innehåller hela det nya meddelandet -> vi får nya `id` + `web_url` direkt.
- **Ingen backdatering:** dokumentationen säger uttryckligen att `created_at`/
  `published_at` **inte** stöds - alla inlägg får nutidsstämpel. (Bekräftar §4.)
- **Ingen redigering:** det finns **inget** PUT/PATCH för meddelanden. Endast skapa,
  läsa, svara, **radera**. (Avgör §5 - se den uppdaterade noten där.)
- **Radering finns** (`DELETE groups/<id>.json`, `DELETE messages/<id>.json`) -> en
  privat PoC-grupp är **fullt återställbar** (flippar "irreversibel"-domen för PoC:n).

Källor: Microsoft Learn `messages-json-post`, `groups.json`-tråden, communityhub om
saknad edit-endpoint.

Kvarstår att verifiera **live** (kräver smoke-test, §8): att den fångade token
faktiskt har **skrivbehörighet**, och skriv-**rate-limit** i praktiken.

## B. Byggd PoC-importer (`scraper/importer.py`)

Tar precis den smala väg §0/§5/§9 rekommenderar - ett privat, raderbart test-community,
en lagom liten källcommunity, kronologisk postning, mentions som text:

- **Kommandon:** `dry-run <gid>` (renderar exakt brödtext till
  `data/import/dryrun_<gid>.txt`, inga skrivningar), `smoke <gid>` (skapar privat
  grupp + 1 trådstart + 1 svar, verifierar skriv-primitiven), `run <gid>` (hela
  communityt, resume via id-map), `teardown <gid>` (DELETE av PoC-gruppen + id-map).
- **Text-transform:** `body.rich` plattas till ren text; **mentions -> `@Namn` som
  text** (aldrig riktig tagg -> ingen avisering - och visar samtidigt att taggning
  *hade* gått), taggar -> `#namn`, interna tråd-länkar -> läs-arkivet.
- **Inbäddning (§4):** prefix *"Ursprungligen av <namn> · <datum>"* + sidfot med
  reaktioner/upvotes/"sett av N"/bästa-svar-markering.
- **Skriv-klient:** `yammer.post()`/`yammer.delete()` återanvänder self-heal-token +
  429/5xx-backoff, med en **adaptiv skriv-throttle** (AIMD): minskar intervallet vid
  varje lyckad skrivning, ökar det multiplikativt vid 429 - söker sig till rätt takt
  (golv 0,8 s, tak 15 s) i stället för en fast gissning.
- **id-map på disk** (`data/import/idmap_<gid>.json`): idempotens + resume (token går
  ut mitt i en körning), och `replied_to_id` mappas gammalt->nytt id.
- **Identitet:** posterna skapas under den inloggades namn med nutidsstämpel (token =
  personlig session) - därav prefixet. Privat enmans-community = inga notiser till andra.

**Status:** dry-run körd mot "TV-spelsgruppen" (47 trådar, 218 inlägg) - rendering OK.
Live-skrivning (smoke -> run) inväntar godkännande + att token visar sig ha skrivrätt.
