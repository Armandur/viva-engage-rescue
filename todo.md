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

## Strukturera befintlig rådata (ingen ny hämtning)
- [ ] @-mentions: bygg tabell message_id -> user_id ur `notified_user_ids`
      (finns även i body.rich som data-yammer-object='user:ID').
- [ ] Likes: bygg tabell ur `liked_by` (antal + vilka som gillat).
- [ ] Visa mentions/likes i trådvyn. (Rasmus 2026-06-09)

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
