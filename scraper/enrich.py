"""Berikningspass: hämtar reaktioner + seenByCount via det moderna GraphQL-API:t
och sparar distillerat per tråd till data/raw/reactions/{thread_id}.json.

Strategi (verifierad mot live-API:t 2026-06-09):
  - NestedThreadClients per tråd -> seenByCount + startinlägg + toppsvar (<=20)
    med reaktioner och cursor per toppsvar.
  - TopLevelRepliesClients paginerar resten av toppsvaren (bara ~2 trådar har >20).
  - Jämför fångade meddelande-id mot v1:s id per tråd. Saknas något ligger det som
    GraphQL-andra-nivå-svar -> SecondLevelRepliesClients per toppsvar tills allt
    täckts. (GraphQL plattar ut gamla trådar, så de flesta behöver inte detta.)

Resume via .done-markör per tråd. scraper/build.py konsumerar filerna.

Kör: uv run python -m scraper.enrich
"""

import json
import sqlite3
import sys
from pathlib import Path

from . import config
from . import graphql as gq
from .yammer import TokenExpired

RAW = Path("data/raw/reactions")
DB = Path("data/archive.db")
SORT = "UPVOTE_RANK_THEN_CREATED_AT"


def _ntc_vars(tid: int) -> dict:
    return {
        "threadId": gq.gid("Thread", tid), "sortRepliesBy": SORT,
        "includeHiddenForNetworkInDiscovery": True, "includeSenderBadges": True,
        "includeOriginNetworkBadge": True, "includeUserHideFields": True,
        "includeSharedMessageAttachments": False, "includeImageArtifactFields": False,
        "includeViewerHasBookmarked": True, "replyCount": 20,
        "includeModerationState": True, "includeMessageContentSourceFile": True,
        "includeSharePointNewsPost": False, "includeVerifiedReply": True,
        "includeViewerCanPrivateReply": True, "requestContentInTargetLanguage": True,
        "contentTargetLanguage": "sv-se",
    }


def _top_vars(tid: int, before: str) -> dict:
    return {
        "threadId": gq.gid("Thread", tid), "last": 20, "before": before,
        "sortRepliesBy": SORT, "includeSenderBadges": True,
        "includeOriginNetworkBadge": True, "includeSharePointNewsPost": False,
    }


def _slr_vars(tid: int, parent_gid: str, parent_cursor: str, before: str | None) -> dict:
    return {
        "threadId": gq.gid("Thread", tid), "topLevelMessageId": parent_gid,
        "topLevelMessageCursor": parent_cursor, "last": 20, "before": before,
        "includeSenderBadges": True, "includeOriginNetworkBadge": True,
        "includeSharePointNewsPost": False,
    }


def _parse_msg(node: dict) -> tuple[str, dict, dict]:
    """Distillerar en meddelande-nod -> (msg_id, {reactions, reactors}, users)."""
    mid = gq.gid_decode(node["id"])
    rc = node.get("reactionsConnection") or {}
    reactions = {k[:-5]: v for k, v in rc.items()
                 if k.endswith("Count") and k != "totalCount"
                 and isinstance(v, int) and v > 0}
    reactors, users = {}, {}
    for k, v in node.items():
        if k == "featuredReactions" or not k.endswith("FeaturedReactions"):
            continue
        if not isinstance(v, dict):
            continue
        typ = k[:-len("FeaturedReactions")]
        ids = []
        for e in v.get("edges", []):
            u = e.get("node") or {}
            try:
                uid = int(gq.gid_decode(u["id"]))
            except Exception:
                uid = u.get("databaseId")
            if uid is None:
                continue
            uid = int(uid)
            ids.append(uid)
            users[uid] = {"name": u.get("displayName"), "email": u.get("email"),
                          "job_title": u.get("jobTitle")}
        if ids:
            reactors[typ] = ids
    # Upvotes (frågetrådar): totalCount = fullt antal, edges = urval av upvoters.
    up = node.get("featuredQuestionReplyUpvotes") or {}
    up_count = up.get("totalCount") or 0
    up_ids = []
    for e in up.get("edges", []):
        u = e.get("node") or {}
        try:
            uid = int(gq.gid_decode(u["id"]))
        except Exception:
            uid = u.get("databaseId")
        if uid is None:
            continue
        uid = int(uid)
        up_ids.append(uid)
        users.setdefault(uid, {"name": u.get("displayName"), "email": u.get("email"),
                               "job_title": u.get("jobTitle")})
    rec = {"reactions": reactions}
    if reactors:
        rec["reactors"] = reactors
    if up_count:
        rec["upvotes"] = {"count": up_count, "upvoters": up_ids}
    return mid, rec, users


def _chase_second(tid: int, parent_mid: str, parent_cursor: str, take) -> None:
    """Hämtar och paginerar alla andra-nivå-svar under ett toppsvar."""
    parent_gid = gq.gid("Message", parent_mid)
    before = None
    while True:
        d = gq.query("SecondLevelRepliesClients",
                     _slr_vars(tid, parent_gid, parent_cursor, before))["thread"]
        conn = None
        for e in (d.get("topLevelRepliesAtMessage") or {}).get("edges", []):
            conn = e.get("secondLevelReplies")
            break
        if not conn:
            return
        for e in conn.get("edges", []):
            take(e["node"])
        pi = conn.get("pageInfo") or {}
        if not pi.get("hasPreviousPage"):
            return
        before = pi["startCursor"]


def _reply_id(wrapper: dict | None) -> int | None:
    """Meddelande-id ur trådens bestReply/verifiedReply ({markedBy, message, ...})."""
    msg = (wrapper or {}).get("message") or {}
    if msg.get("id"):
        try:
            return int(gq.gid_decode(msg["id"]))
        except Exception:
            pass
    tid = msg.get("telemetryId") or msg.get("databaseId")
    return int(tid) if tid else None


def _enrich_thread(tid: int, expected: set[str]) -> dict:
    out_msgs: dict[str, dict] = {}
    users: dict[int, dict] = {}
    cursors: dict[str, str] = {}  # toppsvars msg-id -> edge-cursor

    def take(node: dict) -> str:
        mid, rec, us = _parse_msg(node)
        out_msgs[mid] = rec
        users.update(us)
        return mid

    th = gq.query("NestedThreadClients", _ntc_vars(tid))["thread"]
    seen = th.get("seenByCount")
    best = _reply_id(th.get("bestReply"))
    verified = _reply_id(th.get("verifiedReply"))
    if th.get("threadStarter"):
        take(th["threadStarter"])
    tl = th.get("topLevelReplies") or {}
    for e in tl.get("edges", []):
        cursors[take(e["node"])] = e.get("cursor")
    pi = tl.get("pageInfo") or {}
    while pi.get("hasPreviousPage"):
        d = gq.query("TopLevelRepliesClients", _top_vars(tid, pi["startCursor"]))["thread"]
        tl2 = d.get("topLevelReplies") or {}
        for e in tl2.get("edges", []):
            cursors[take(e["node"])] = e.get("cursor")
        pi = tl2.get("pageInfo") or {}

    if expected - set(out_msgs):  # några v1-meddelanden saknas -> andra-nivå
        for pmid, pcur in list(cursors.items()):
            if not pcur:
                continue
            _chase_second(tid, pmid, pcur, take)
            if not (expected - set(out_msgs)):
                break

    return {"thread_id": int(tid), "seen_by_count": seen,
            "best_reply_id": best, "verified_reply_id": verified,
            "messages": out_msgs, "users": users}


def main() -> None:
    if not DB.exists():
        raise SystemExit("Bygg arkivet först (python -m scraper.build) - tråd-/v1-id behövs.")
    RAW.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    sel = config.selected_groups()
    if sel is not None:
        ph = ",".join("?" * len(sel))
        threads = [r[0] for r in con.execute(
            f"SELECT DISTINCT thread_id FROM messages WHERE group_id IN ({ph}) "
            "ORDER BY thread_id DESC", list(sel))]
        print(f"Selektiv berikning: {len(threads)} trådar i valda communities.")
    else:
        threads = [r[0] for r in con.execute(
            "SELECT DISTINCT thread_id FROM messages ORDER BY thread_id DESC")]
    expected: dict[int, set[str]] = {}
    for tid, mid in con.execute("SELECT thread_id, id FROM messages"):
        expected.setdefault(tid, set()).add(str(mid))
    con.close()

    # --force: kör om alla trådar trots .done (för att backfilla nya fält som
    # upvotes/bästa-svar i redan berikade trådar). Annars resume via .done.
    force = "--force" in sys.argv
    if force:
        print("--force: kör om även redan berikade trådar (.done ignoreras).")

    total = len(threads)
    done = newly = skipped = 0
    pq_streak = 0  # PersistedQueryGone i rad -> trolig app-deploy, avbryt då
    for i, tid in enumerate(threads, 1):
        if not force and (RAW / f"{tid}.done").exists():
            done += 1
            continue
        try:
            data = _enrich_thread(tid, expected.get(tid, set()))
        except TokenExpired:
            print("Token slut, ingen ny inom tidsgräns - avbryter (kör om för resume).")
            break
        except gq.PersistedQueryGone as e:
            # Hashen kom inte tillbaka trots väntan. Hoppa tråden och fortsätt -
            # håll en Viva-flik öppen så åter-registreras queryn. Resume tar dem.
            pq_streak += 1
            print(f"  tråd {tid}: persisted query borta ({e}) - hoppar (kör om för resume)")
            (RAW / f"{tid}.skipped").write_text(str(e))
            skipped += 1
            if pq_streak >= 15:
                print("15 trådar i rad med borttappad query - trolig app-deploy. "
                      "Avbryter; fånga nya hashar och kör om.")
                break
            continue
        except Exception as e:
            print(f"  tråd {tid}: fel {type(e).__name__}: {e} - hoppar")
            (RAW / f"{tid}.skipped").write_text(str(e))
            skipped += 1
            continue
        pq_streak = 0
        (RAW / f"{tid}.json").write_text(json.dumps(data, ensure_ascii=False))
        (RAW / f"{tid}.done").write_text("")
        done += 1
        newly += 1
        cov, exp = len(data["messages"]), len(expected.get(tid, set()))
        if newly % 25 == 0 or cov < exp:
            print(f"[{i}/{total}] tråd {tid}: {cov}/{exp} meddelanden, seen={data['seen_by_count']}")
    print(f"Klart. {done}/{total} trådar klara ({newly} nya denna körning), {skipped} hoppade.")


if __name__ == "__main__":
    main()
