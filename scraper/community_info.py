"""Hämtar community-info via moderna GraphQL: lång info-text (extendedDescription),
fästa länkar (pinnedObjects) och medlemsantal (memberCount) per community.

Legacy-API:t ger bara den korta `description` - info-panelen och pins finns bara
i `GroupSidebarClients`. Sparar distillerat per community till
data/raw/community_info/{group_id}.json. build.py konsumerar filerna.

featuredMembers (~10 med admin-flagga) lagras som en preview; hela medlemslistan
kräver en separat paginerad query (ej fångad än).

Kör: uv run python -m scraper.community_info
"""

import json
import sqlite3
from pathlib import Path

from . import config
from . import graphql as gq
from . import yammer
from .yammer import Forbidden, TokenExpired

RAW = Path("data/raw/community_info")
DB = Path("data/archive.db")


def _members_legacy(gid: int) -> list[dict]:
    """Roster via dokumenterad legacy-endpoint users/in_group (paginerad på
    sid-fullhet). Stabil men saknar admin-flagga. Tom vid Forbidden."""
    members, page = [], 1
    while True:
        try:
            d = yammer.get(f"users/in_group/{gid}.json", page=page)
        except Forbidden:
            break
        us = d.get("users", []) if isinstance(d, dict) else []
        for u in us:
            members.append({
                "user_id": u["id"], "name": u.get("full_name") or u.get("name"),
                "email": u.get("email"), "job_title": u.get("job_title"),
                "is_admin": None,
            })
        if len(us) < 50:
            break
        page += 1
    return members


def _members(gid: int) -> list[dict]:
    """Hela medlemslistan MED admin-flagga via GroupMemberPanelClients (paginerad
    via after = sista edgens cursor; pageInfo har bara hasNextPage). Faller
    tillbaka på stabil legacy-roster (utan admin) om persisted query dör."""
    g = gq.gid("Group", gid)
    members: list[dict] = []
    after = None
    try:
        while True:
            v = {"groupId": g, "includeOriginNetworkBadge": True}
            if after:
                v["after"] = after
            conn = ((gq.query("GroupMemberPanelClients", v).get("group") or {})
                    .get("members") or {})
            edges = conn.get("edges") or []
            for e in edges:
                n = e.get("node") or {}
                try:
                    uid = int(gq.gid_decode(n["id"])) if n.get("id") else n.get("databaseId")
                except Exception:
                    uid = n.get("databaseId")
                if uid is None:
                    continue
                members.append({
                    "user_id": int(uid), "name": n.get("displayName"),
                    "email": n.get("email"), "job_title": n.get("jobTitle"),
                    "is_admin": bool(e.get("isAdmin")),
                })
            if not edges or not (conn.get("pageInfo") or {}).get("hasNextPage"):
                break
            after = edges[-1].get("cursor")
            if not after:
                break
        return members
    except gq.PersistedQueryGone:
        print(f"  GroupMemberPanelClients borta för {gid} - faller tillbaka på legacy (utan admin)")
        return _members_legacy(gid)


def _sidebar(gid: int) -> dict:
    return gq.query("GroupSidebarClients", {
        "groupId": gq.gid("Group", gid), "includeGroupCampaignInfo": True,
        "includeGroupLeaders": False, "includeNetworkLeaderFieldsInGroupLeaders": False,
    })


def _distill(gid: int, g: dict) -> dict:
    pinned = []
    for e in ((g.get("pinnedObjects") or {}).get("edges") or []):
        n = e.get("node") or {}
        pinned.append({
            "title": e.get("pinnedLinkTitle") or n.get("title"),
            "url": n.get("url"), "type": n.get("__typename"),
            "description": n.get("description"),
        })
    members = []
    for e in ((g.get("featuredMembers") or {}).get("edges") or []):
        n = e.get("node") or {}
        try:
            uid = int(gq.gid_decode(n["id"])) if n.get("id") else n.get("databaseId")
        except Exception:
            uid = n.get("databaseId")
        if uid is not None:
            members.append({"user_id": int(uid), "name": n.get("displayName"),
                            "is_admin": bool(e.get("isAdmin"))})
    return {
        "group_id": gid,
        "extended_description": g.get("extendedDescription"),
        "member_count": (g.get("memberCount") or {}).get("totalCount"),
        "pinned": pinned, "featured_members": members,
    }


def main() -> None:
    if not DB.exists():
        raise SystemExit("Bygg arkivet först (python -m scraper.build).")
    RAW.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    sel = config.selected_groups()
    if sel is not None:
        gids = [g for g in sel]
    else:
        gids = [r[0] for r in con.execute("SELECT id FROM communities ORDER BY id")]
    con.close()

    total = len(gids)
    done = skipped = pq = 0
    for i, gid in enumerate(gids, 1):
        out = RAW / f"{gid}.json"
        if out.exists():
            done += 1
            continue
        try:
            g = (_sidebar(gid).get("group")) or {}
        except TokenExpired:
            print("Token slut - avbryter (kör om för resume).")
            break
        except gq.PersistedQueryGone:
            pq += 1
            print(f"  community {gid}: persisted query borta - hoppar")
            if pq >= 15:
                print("15 i rad borta - trolig app-deploy. Avbryter.")
                break
            continue
        except Exception as e:
            print(f"  community {gid}: fel {type(e).__name__}: {e} - hoppar")
            skipped += 1
            continue
        pq = 0
        data = _distill(gid, g)
        try:
            data["members"] = _members(gid)
        except TokenExpired:
            print("Token slut - avbryter (kör om för resume).")
            break
        data["admin_count"] = sum(1 for m in data["members"] if m.get("is_admin"))
        out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        done += 1
        if data["members"] or data["pinned"]:
            print(f"[{i}/{total}] community {gid}: {len(data['members'])} medlemmar "
                  f"({data['admin_count']} admins), {len(data['pinned'])} pins")
    print(f"Klart. {done}/{total} klara, {skipped} hoppade.")


if __name__ == "__main__":
    main()
