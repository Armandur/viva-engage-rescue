# Att göra

## Datatäckning att utöka
- [ ] **Full användarmetadata.** Meddelande-feeden ger redan name (handle), avatar
      (mugshot_url), state, aad_guest, activated_at - lagras nu. Återstår: hämta
      komplett medlemslista via `/users.json` och full profil via `/users/{id}.json`
      (ort, avdelning, bio m.m.), samt ladda ner avatarer lokalt. (Rasmus 2026-06-09)
- [ ] **Användares egna flöden ("Storylines").** Viva Engage har personliga flöden
      likt en vägg där användare publicerar utanför communities. Hämta dessa
      (sannolikt `messages/from_user/{id}` eller storyline-API - undersök) så även
      de inläggen/kommentarerna arkiveras. (Rasmus 2026-06-09)
- [ ] **Redigerade inlägg/kommentarer.** API:t ger bara aktuell text, ingen
      edited_at och ingen historik - redigeringshistorik går alltså inte att fånga.
      Sluttillståndet fångas däremot genom att köra hela pipelinen en sista gång
      så nära nedstängningen som möjligt (bygget dedupar på id och tar senaste
      rådata). (Rasmus 2026-06-09)

## Huvudleverans
- [ ] Sökbart arkiv: läs `data/raw/` -> SQLite + FTS5 (dedup på message-id),
      tabeller för communities/users/messages/attachments.
- [ ] Bläddrings-/sök-UI ovanpå arkivet (community -> tråd -> meddelanden, fritextsök).
- [ ] Kör bilage-passet (`download.py`) klart så filer säkras innan nedstängning.

## UI
- [ ] Panelen och arkiv-UI:t är handrullad CSS just nu. Om det underlättar när
      vyerna växer: ta in Bootstrap (eller annat lämpligt lib) i stället för att
      bygga mer egen CSS. (Önskemål från Rasmus 2026-06-09.)
- [ ] Trådvyn visar svar platt och kronologiskt. Visualisera nästling med indrag
      och färgade nivålinjer till vänster (strukturen finns via replied_to_id).
      (Önskemål från Rasmus 2026-06-09.)

## Token / drift
- [ ] IndexedDB-skanning i userscriptet är sista försöket till auto-fångst i nya
      Viva. Lyckas det inte: kör manuell inklistring (1-2 ggr räcker för full dump).
- [ ] Ev. watchdog som auto-återupptar dumpen när färsk token finns i `.env`.
- [ ] Ompröva inkrementellt läge (--update): det bygger på in_group, som inte är
      en komplett trådfeed. För komplett uppdatering måste trådar med ny aktivitet
      även backfillas om via in_thread. Nuvarande --update fångar inte allt.

## Klart
- [x] Dump av alla communities med resume (.done/.cursor) och nätverks-retry.
- [x] Inkrementellt läge (--update) som även fångar svar på gamla trådar.
- [x] Kontrollpanel: start/stopp, token-byte, progress, datastorlek.
- [x] Token-sync-userscript (audience-filtrerat) + servering via panelen.
