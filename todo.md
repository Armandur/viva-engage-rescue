# Att göra

## Publicering
- [x] **Admin och arkiv separerade på olika appar/portar (2026-06-14).**
      `app/main.py` = admin/kontrollpanel (styrning, token, status) på port 8050.
      `app/public.py` = publik läs-bar app som serverar ENBART /arkiv, på egen
      port (8051). `app/archive.py` är en fristående router som båda inkluderar.
      Det är `public`-appen som publiceras externt; admin exponeras aldrig.
- [ ] **Extern PoC-publicering bakom åtkomstskydd.** Kör `app.public` bakom Nginx
      Proxy Manager med basic auth som första PoC. Admin-panelen hålls internt.
- [ ] **Riktig användarhantering (när PoC:n ska bli mer än PoC).** Ersätt
      basic-auth-i-proxy med inloggning + roller i appen (inte bara basic auth).
      Byggs när behovet är konkret.
- [x] **Containeriserad (2026-06-14).** Dockerfile (uv + python:3.12-slim, kör
      `app.public` som standard), `docker-compose.yml` (publika arkivet på 8051;
      admin bakom compose-profil "admin" på 8050, ej default), `docker-compose.dev.yml`
      (lokal bygg + reload), GitHub Actions → `ghcr.io/armandur/viva-engage-rescue`,
      `.dockerignore`, `DOCKER.md`. Imagen byggd + smoke-testad lokalt: publika
      appen svarar 200, admin (`/api/status`) ger 404 i publika imagen. Publika
      arkivet monterar `data/` read-only; admin-profilen rw + `.env`.
- [ ] **Driftsätt på Unraid + importera data.** Pusha repot (CI bygger/publicerar
      imagen), kör `docker-compose.yml` på TERVO2 med volymen mot en host-path
      (t.ex. /mnt/user/appdata/viva-arkiv/data), `rsync` över `data/` (~28 GB,
      efter sista omkörningen så mest kompletta datan importeras), och sätt basic
      auth i Nginx Proxy Manager framför port 8051.

## Dokumentation / kunskapsdelning
- [x] **Self-contained HTML-rapport om API:erna vi använt (2026-06-14).** Skapad:
      `viva-api-rapport.html` (~19 KB, inline CSS, inga externa beroenden, sticky
      innehållsförteckning). Täcker REST + GraphQL/APQ, alla fångade hashar,
      fallgropar (older_available, featuredReactions cap 8, signerade URL:er,
      cursor-riktning, zombies) samt datamodell + rekommenderade mönster.
      URSPRUNGLIG SPEC: En enda fristående .html-fil (inline CSS/JS, inga beroenden)
      som andra kan öppna och lära av.
      Ska gå igenom, med konkreta exempel (request + svarsutdrag):
      - **Legacy Yammer REST** (`www.yammer.com/api/v1`): messages/in_group,
        messages/in_thread, users/in_group, users.json, groups.json, bilage-
        nedladdning. Paginering på sid-fullhet (older_than) + att `older_available`
        är opålitlig. Throttle/429/5xx-hantering, self-heal-token.
      - **Modern Viva GraphQL** (`engage.cloud.microsoft/graphql`, Apollo persisted
        queries): operationName + sha256Hash + variables (ingen query-text),
        base64-nod-id (`{_type,id}`), och de hashar vi fångat: NestedThreadClients,
        TopLevelRepliesClients, SecondLevelRepliesClients, FeedUserWallNestedClients,
        FeedStorylineAllNestedClients, GroupSidebarClients, GroupMemberPanelClients,
        MessageReactionsClients. Cursor-riktning (startCursor/hasPreviousPage vs
        endCursor/hasNextPage) och att hashar dör vid app-deploy.
      - **Lärdomar/fallgropar:** featuredReactions = bara urval (max 8), full lista
        via MessageReactionsClients; signerade mugshot/thumbnail-URL:er går ut
        (ladda via redirect/lokalt); storyline-feeden paginerar bakåt; token
        fångas från webbsessionen (kortlivad).
      - **Förslag på strukturer/upplägg:** SQLite-schemat, råd-dump -> build-pipeline,
        resume-mönster, separation admin/publik, lazy-loading + render_body.
      Mål: någon annan ska kunna återskapa angreppssättet mot Viva/Yammer.
Själva insamlingen är i praktiken klar (meddelanden, trådar, reaktioner+seen,
storylines, community-info, profiler, avatarer, bilagor). Kvar:
- [ ] **Säkerhetskopiera `data/`** bort från VM:ens enda lokala disk. Allt arbete
      (rådata + byggd db + bilagor + avatarer) ligger på en disk. Tidskänsligt.
- [ ] **En sista full omkörning nära nedstängningen** så sent som möjligt.
      Görs nu med ETT klick: panelknappen "Kör hela kedjan" (scraper/pipeline.py)
      kör dump->threads->build->storylines->build->enrich->community_info->users->
      download->build i serie, resumebart och self-heal-token. Håll en Viva-flik
      öppen så token matas automatiskt; kedjan rullar utan tillsyn i timmar.
      Berikningen (GraphQL persisted hashes) sker i samma svep nära fångsten.

## Användbarhet
- [x] **Selektiv körning per community.** `--groups id1,id2`-argument
      (config.selected_groups()) i dump/threads/download/enrich + multiselect i
      panelen ("Begränsa till vissa communities") som skickar ?groups= till
      /start. Tomt = alla. (2026-06-10)
- [x] **Arkiv-UX: datum, sökfilter, översikt.** Svensk lokaltid (svtid-filter);
      sök filtrerbart på community/avsändare/datumintervall (+ filter-only
      bläddring, säker snippet); översiktssida (/arkiv/oversikt) med totaler,
      toppcommunities/-avsändare och klickbara år -> listar årets inlägg via
      datumfiltret (browse-gräns 1000). (2026-06-10)

## Datatäckning att utöka
- [x] **Full användarmetadata (2026-06-12).** `scraper/users.py`: legacy
      `users.json` ger SAMMA fält som `/users/{id}.json` (ort/avdelning/bio/
      expertis/telefon/mugshot), så ett enda paginerat svep (50/sida, resume via
      users_roster.state.json) hämtar hela nätverkets fulla profiler ->
      data/raw/users/{id}.json. Avatarfasen laddar mugshot bara för
      arkivdeltagare (avsändare ∪ nämnda ∪ gillare ∪ reagerande ∪ medlemmar) ->
      data/avatars/{id}.<ext> (idempotent). build.py utökar users-schemat
      (location/department/summary/expertise/interests/hire_date/birth_date/phone/
      timezone/network_name/avatar_local/raw_json), INSERT OR IGNORE för medlemmar
      som aldrig postat + UPDATE av alla profilfält. PII: hela profilen sparas
      (raw_json) enligt beslut 2026-06-12. UTFALL: 33 663 fulla profiler hämtade
      (= nätverkets totala användarantal).
      AVATAR-SCOPE: bara genuint aktiva (avsändare ∪ nämnda ∪ gillare ∪
      reagerande) = 5 046 - INTE group_members, eftersom de stora communityn
      ("Anställd i Svenska kyrkan" ~11k m.fl.) drar in nästan hela org och rena
      medlemmar visas inte med bild någonstans. Utfall: 767 med foto, resten
      no_photo. VISNING: /arkiv/avatar/{uid} ger riktig mugshot om den finns,
      annars en genererad SVG-initialcirkel (deterministisk färg) - så avatar
      visas konsekvent i trådvy (per meddelande), användarsida och panel-ticker
      utan att vyerna behöver känna till om en bild finns. Panel: knapp
      "Användarinfo + avatarer", progress (roster = striped, avatarer = %-stapel)
      + höger-till-vänster avatar-ticker (rAF-marquee, en token per unik avatar).
- [x] **Användares egna flöden ("Storylines").** KÖRT + KLART. `scraper/storylines.py`: upptäcker
      storyline-tråd-id per användare via `FeedUserWallNestedClients`
      (data.user.wallFeed.threads, hash i graphql.HASHES, paginering bakåt via
      `olderThan` = föregående sidas startCursor tills hasPreviousPage=False) och
      backfillar dem via threads._backfill (in_thread NÅR dem, group_id=None).
      build.py parsar dem som vanligt; enrich ger reaktioner/seen. Panelknapp
      "Storylines" + kommando inkopplat. UI klart: användarsidan /arkiv/u/{id}
      har flikar Storyline (group_id IS NULL) + I communities.
      KÖRORDNING: enrich klart -> Storylines -> bygg om -> (enrich nya trådar) ->
      bilagor. Delar GraphQL-rate-limit med enrich, så kör inte samtidigt.
- [x] **Berika med reaktioner + seen-count via modernt GraphQL-API.** KÖRT +
      KLART (8 858 trådar med seen, 17 001 reaktionsrader). `scraper/graphql.py` (persisted-query-klient, self-heal, 5xx-backoff) +
      `scraper/enrich.py` (NestedThreadClients -> TopLevelRepliesClients ->
      SecondLevelRepliesClients; jämför fångade id mot v1 per tråd och jagar bara
      andra-nivå där något saknas; resume via .done per tråd ->
      data/raw/reactions/). build.py fyller tabellerna reactions/reactors/
      thread_meta; trådvyn visar emoji per typ + reagerande + "Sett av N".
      Panelknapp "Berika". Verifierat 100% täckning på test-trådar (modern m.
      andra-nivå 67/67). Obs: persisted-hashar dör vid app-deploy -> engångskör
      nära fångsten, INTE med i den omkörbara v1-pipelinen.
      KÖRORDNING: trådfasen klar -> bygg om -> enrich -> bilagor.
- [ ] **Redigerade inlägg/kommentarer.** API:t ger bara aktuell text, ingen
      edited_at och ingen historik - redigeringshistorik går alltså inte att fånga.
      Sluttillståndet fångas däremot genom att köra hela pipelinen en sista gång
      så nära nedstängningen som möjligt (bygget dedupar på id och tar senaste
      rådata). (Rasmus 2026-06-09)

## Strukturera befintlig rådata (ingen ny hämtning)
- [x] **Community-vyn visar inläggstyp.** Fråga/📣 Meddelande/Omröstning-badge +
      ev. rubrik per tråd (trådstartarens message_type/title). (2026-06-10)
- [x] **Saknade trådstarter - utrett + delvis BUGG-FIX (2026-06-10).** 22 av 8564
      trådar saknade startmeddelandet. Visade sig vara TVÅ orsaker: (a) 19 genuint
      raderade i källan (startaren ger 404), (b) 3 var en PAGINERINGSBUGG: Yammers
      `meta.older_available` kan vara False trots att äldre meddelanden finns, och
      vår iter trodde på den -> trunkerade trådar (tappade äldsta = startaren).
      Fixat i yammer.py: `_iter_message_pages` paginerar nu på sid-fullhet
      (<limit = klart) i stället för older_available, med no-progress-skydd.
      Gäller både in_thread OCH in_group. De 22 omkörda -> 3 startare (+ ev. fler
      äldre meddelanden) återställda. OBS: in_group kan ha missat hela trådar av
      samma skäl -> en omkörning av dump/threads med fixen kan fånga mer (men
      kräver token; trunkering = saknad startare, så de 19 kvar är genuint raderade).
- [x] **Tidig aktivitet 2008-2012 stämmer (2026-06-10).** Kontrollerat: äkta
      inlägg (t.ex. "Först! Och kanske sist." 2008-09-12 = nätverkets första).
      Svenska kyrkan tidig Yammer-användare; glest 2008-2012, växte sedan.
- [x] @-mentions: tabell message_id -> user_id ur `notified_user_ids`.
- [x] Likes: tabell ur `liked_by` (antal + vilka som gillat).
- [x] Visa mentions/likes i trådvyn.
- [x] **Meddelandetext renderas från `rich`** med strikt allowlist-sanerare
      (`_RichSanitizer` i app/archive.py, stdlib HTMLParser). Behåller
      formatering (br, i, strong, p, hr) som parsed tappar, skriver om
      yammer-object-spans till interna länkar (user -> /arkiv/u/{id},
      group -> /arkiv/c/{id}, tag -> #namn) och behåller externa länkar med
      säker href. Testat över hela korpusen (21106 meddelanden, 0 fel/tomma).
      body_parsed/body_urls/tags lagras fortfarande som data men är inte på
      renderingsvägen. (Parsed plattade radbrytningar/formatering, t.ex. för
      HTML-postade driftbot-meddelanden - därav bytet.)

## Huvudleverans
- [x] Sökbart arkiv: läs `data/raw/` -> SQLite + FTS5 (dedup på message-id),
      tabeller för communities/users/messages/attachments.
- [x] Bläddrings-/sök-UI ovanpå arkivet (community -> tråd -> meddelanden, fritextsök).
- [x] **Bilage-passet (`download.py`) KÖRT.** 1 735 filbilagor nedladdade till
      `data/attachments/` (alla med local_path i DB), nekade loggade till
      data/attachments_denied.json + visas i /arkiv/nekade. Bör köras om en sista
      gång nära nedstängningen för att fånga nytillkomna filer (se Operativt kvar).

## UI
- [x] Trådvyn visualiserar nästling med indrag och färgade nivålinjer (lvl1-5)
      ur replied_to_id-kedjan.
- [x] Bootstrap 5 (CDN) infört i panel + arkiv-UI.
- [x] Användarsida /arkiv/u/{id}: profil + senaste inlägg (mål för @-mentions).
- [x] Exempelsida /arkiv/exempel: direktlänkar till ett exempel per specialfall.
- [x] **Avatar överallt + SVG-fallback (2026-06-12/13).** /arkiv/avatar/{uid} ger
      riktig mugshot eller genererad SVG-initialcirkel; visas i trådvy/användarsida/
      community-medlemstabell. Admins som egna kort (avatar+namn+titel).
- [x] **Viva-likt: inline-media + länkkort (2026-06-13).** Bildbilagor (type=image)
      renderas inline (miniatyr -> full), delade länkar (ymodule) som länkkort
      (titel/beskrivning/domän via attachments.description, ny kolumn).
      Domän-filter i archive.py. Video utelämnat (0 nedladdade); omröstnings-
      staplar utelämnat (sällsynt + röstsiffror ej fångade).

## Token / drift
- [x] Token-fångst löst: userscript v1.7 hookar sidans fetch (unsafeWindow) och
      matar panelen automatiskt; self-heal-klienten läser .env per anrop. Hands-off
      så länge en Viva-flik är öppen. (Verifierat 2026-06-09 - 42 token fångade)
- [ ] Ompröva inkrementellt läge (--update): det bygger på in_group, som inte är
      en komplett trådfeed. För komplett uppdatering måste trådar med ny aktivitet
      även backfillas om via in_thread. Nuvarande --update fångar inte allt.

## Klart
- [x] Dump av alla communities med resume (.done/.cursor) och nätverks-retry.
- [x] Inkrementellt läge (--update) som även fångar svar på gamla trådar.
- [x] Kontrollpanel: start/stopp, token-byte, progress, datastorlek.
- [x] Token-sync-userscript (audience-filtrerat) + servering via panelen.
