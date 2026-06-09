# Att göra

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

## Klart
- [x] Dump av alla communities med resume (.done/.cursor) och nätverks-retry.
- [x] Inkrementellt läge (--update) som även fångar svar på gamla trådar.
- [x] Kontrollpanel: start/stopp, token-byte, progress, datastorlek.
- [x] Token-sync-userscript (audience-filtrerat) + servering via panelen.
