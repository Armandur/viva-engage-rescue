"""Upptäcker storyline-trådar (väggposter) via det org-övergripande storyline-
flödet och backfillar dem via legacy in_thread (samma råformat som threads.py, så
build.py parsar dem utan ny kod; storyline-poster får group_id=None).

Tidigare probades varje konto separat (FeedUserWallNestedClients) - med hela
rostern (~34k) blev det tiotusentals nästan-tomma anrop. Nu paginerar vi i
stället `FeedStorylineAllNestedClients` (Vivas "alla"-storyline-flöde), vars
noder ÄR trådar - så vi får bara faktiska storyline-trådar, oberoende av antal
konton. Paginering bakåt via `olderThan` = föregående sidas pageInfo.startCursor
tills hasPreviousPage=False (samma cursor-riktning som per-konto-väggen; det är
startCursor/hasPreviousPage som driver flödet, inte endCursor/hasNextPage).

Kör: uv run python -m scraper.storylines
"""

import json
from pathlib import Path

from . import graphql as gq
from . import threads
from .yammer import Forbidden, TokenExpired

RAW = Path("data/raw")
DISCOVERED = RAW / "storyline_threads.json"      # {threads: [tid,...], cursor, complete}
PROGRESS = Path("data/storyline_progress.json")  # {phase, done, total} för panelen


def _progress(phase: str, done: int, total: int) -> None:
    PROGRESS.write_text(json.dumps({"phase": phase, "done": done, "total": total}))


def _feed_vars(older_than: str | None) -> dict:
    return {
        "threadCount": 20, "replyCount": 2,
        "sortRepliesBy": "UPVOTE_RANK_THEN_CREATED_AT", "sortThreadsBy": "CREATED_AT",
        "olderThan": older_than,  # null = nyaste sidan; annars föregående endCursor
        "includeSharePointNewsPost": False, "requestContentInTargetLanguage": True,
        "contentTargetLanguage": "sv-se", "includeHiddenForNetworkInDiscovery": True,
        "includeViewerHasBookmarked": True, "includeOriginNetworkBadge": True,
        "includeUserHideFields": True, "includeSharedMessageAttachments": False,
        "includeImageArtifactFields": False,
    }


def _load_state() -> tuple[set[int], str | None, bool]:
    """Returnerar (funna tråd-id, cursor att fortsätta från, klar?). Migrerar det
    gamla per-konto-formatet {user_id: [tids]} till en platt trådmängd."""
    if not DISCOVERED.exists():
        return set(), None, False
    try:
        d = json.loads(DISCOVERED.read_text())
    except (ValueError, OSError):
        return set(), None, False
    if isinstance(d, dict) and "threads" in d:
        return set(d.get("threads") or []), d.get("cursor"), bool(d.get("complete"))
    # gammalt format: {user_id: [tids]} -> behåll trådarna, börja om pagineringen
    found: set[int] = set()
    if isinstance(d, dict):
        for v in d.values():
            for t in (v or []):
                found.add(int(t))
    return found, None, False


def _save_state(found: set[int], cursor: str | None, complete: bool) -> None:
    DISCOVERED.write_text(json.dumps(
        {"threads": sorted(found), "cursor": cursor, "complete": complete}))


def _discover() -> list[int]:
    """Paginerar org-storyline-flödet och samlar alla tråd-id. Resumebart via
    sparad cursor; idempotent (mängd)."""
    found, cursor, complete = _load_state()
    if complete:
        print(f"Upptäckt redan klar ({len(found)} storyline-trådar).")
        return sorted(found)
    pq_streak = 0
    while True:
        try:
            d = gq.query("FeedStorylineAllNestedClients", _feed_vars(cursor))
        except TokenExpired:
            print("Token slut under upptäckt - avbryter (kör om för resume).")
            break
        except gq.PersistedQueryGone:
            pq_streak += 1
            print("  storyline-flödets query borta - väntar/hoppar")
            if pq_streak >= 5:
                print("Queryn kom inte tillbaka - trolig app-deploy. Avbryter.")
                break
            continue
        pq_streak = 0
        conn = ((d.get("allStorylineFeed") or {}).get("threads") or {})
        before = len(found)
        for e in conn.get("edges", []):
            n = e.get("node") or {}
            tid = n.get("databaseId")
            if tid is None:
                try:
                    tid = int(gq.gid_decode(n["id"]))
                except Exception:
                    continue
            found.add(int(tid))
        pi = conn.get("pageInfo") or {}
        # OBS: flödet paginerar bakåt via startCursor/hasPreviousPage (som per-konto-
        # väggen), INTE endCursor/hasNextPage. olderThan = föregående sidas startCursor.
        new_cursor = pi.get("startCursor")
        _save_state(found, new_cursor, False)
        _progress("discover", len(found), 0)
        print(f"  storyline-flöde: +{len(found) - before} (totalt {len(found)})")
        if not pi.get("hasPreviousPage") or not new_cursor or new_cursor == cursor:
            complete = True
            break
        cursor = new_cursor
    _save_state(found, cursor, complete)
    if complete:
        _progress("discover", len(found), len(found))
    return sorted(found)


def _backfill(thread_ids: list[int]) -> None:
    total = len(thread_ids)
    done = skipped = 0
    for tid in thread_ids:
        tdir = threads.THREADS / str(tid)
        if (tdir / ".done").exists():
            done += 1
        else:
            try:
                threads._backfill(tid)
                done += 1
            except Forbidden:
                tdir.mkdir(parents=True, exist_ok=True)
                (tdir / ".skipped").write_text("ingen åtkomst\n", encoding="utf-8")
                skipped += 1
            except TokenExpired:
                print("Token slut under backfill - avbryter (resume möjlig).")
                return
        if (done + skipped) % 10 == 0:
            _progress("backfill", done + skipped, total)
        if done % 50 == 0:
            print(f"  backfill {done}/{total}")
    _progress("backfill", total, total)
    print(f"Backfill klar: {done} trådar, {skipped} hoppade.")


def main() -> None:
    print("Upptäcker storyline-trådar via org-flödet (alla)...")
    all_tids = _discover()
    print(f"\nUpptäckt: {len(all_tids)} storyline-trådar.")
    print("Backfillar via in_thread (-> data/raw/threads/)...")
    _backfill(all_tids)
    print("Klart. Bygg om arkivet (python -m scraper.build) så de syns under Storyline.")


if __name__ == "__main__":
    main()
