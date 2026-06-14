"""Kompletterar FULLA reaktörslistor där berikningen bara fick urvalet.

Berikningen (enrich) fångar bara `featuredReactions` - Vivas förhandsurval på
max 8 personer per reaktionstyp. Detta pass hittar de meddelanden där en reaktion
har fler reagerande än vad som sparats och hämtar hela listan via
MessageReactionsClients (paginerad, after=endCursor, reaction=null = alla typer),
sedan uppdaterar det data/raw/reactions/{tid}.json så build får med alla.

Naturlig resume: ett meddelande är "kapat" så länge sparade reaktorer < count.
När hela listan hämtats är de lika och hoppas nästa körning. Kör efter enrich.

Kör: uv run python -m scraper.reactors

Obs: läser kapade meddelanden direkt ur reaktions-JSON:en (inte archive.db), så
inget mellanbygge krävs. --groups hanteras inte här - det är ett
fullständighetspass över allt som berikats.
"""

import json
from pathlib import Path

from . import graphql as gq
from .yammer import TokenExpired

RAW = Path("data/raw/reactions")
PROGRESS = Path("data/reactors_progress.json")


def _progress(done: int, total: int, upgraded: int) -> None:
    PROGRESS.write_text(json.dumps({"done": done, "total": total, "upgraded": upgraded}),
                        encoding="utf-8")


def _capped() -> list[tuple[Path, int, list[str]]]:
    """(tråd-json, message_id, giltiga typnycklar) för meddelanden där någon
    reaktion har fler reagerande (count) än sparade reaktor-namn."""
    out = []
    if not RAW.exists():
        return out
    for f in RAW.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for mid_s, rec in (d.get("messages") or {}).items():
            reactions = rec.get("reactions") or {}
            reactors = rec.get("reactors") or {}
            if any(cnt > len(reactors.get(typ, [])) for typ, cnt in reactions.items()):
                out.append((f, int(mid_s), list(reactions.keys())))
    return out


def _react_key(enum: str, valid: list[str]) -> str:
    """GraphQL-enum (LIKE, HEART_BROKEN) -> vår camelCase-nyckel, matchad mot
    meddelandets befintliga count-nycklar så typerna blir konsekventa."""
    norm = (enum or "").replace("_", "").lower()
    for k in valid:
        if k.lower() == norm:
            return k
    parts = (enum or "").lower().split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _full_reactors(mid: int, valid: list[str]) -> tuple[dict, dict]:
    """Hela reaktörslistan för ett meddelande. Returnerar
    ({typ: [user_id,...]}, {user_id: {name,email,job_title}})."""
    msg_gid = gq.gid("Message", mid)
    reactors: dict[str, list[int]] = {}
    users: dict[int, dict] = {}
    after = None
    while True:
        v = {"messageId": msg_gid, "reaction": None, "after": after}
        conn = ((gq.query("MessageReactionsClients", v).get("messageReactions") or {})
                .get("reactionsConnection") or {})
        for e in conn.get("edges") or []:
            n = e.get("node") or {}
            uid = n.get("databaseId")
            if uid is None:
                try:
                    uid = int(gq.gid_decode(n["id"]))
                except Exception:
                    continue
            uid = int(uid)
            typ = _react_key(e.get("reaction") or "", valid)
            reactors.setdefault(typ, []).append(uid)
            users[uid] = {"name": n.get("displayName"), "email": n.get("email"),
                          "job_title": n.get("jobTitle")}
        pi = conn.get("pageInfo") or {}
        if not pi.get("hasNextPage") or not pi.get("endCursor"):
            break
        after = pi["endCursor"]
    # dedupa per typ (skydd mot ev. överlappande sidor)
    return ({t: list(dict.fromkeys(ids)) for t, ids in reactors.items()}, users)


def main() -> None:
    capped = _capped()
    total = len(capped)
    print(f"{total} meddelanden med kapad reaktörslista att komplettera.")
    done = upgraded = pq = 0
    _progress(0, total, 0)
    for f, mid, valid in capped:
        try:
            reactors, users = _full_reactors(mid, valid)
        except TokenExpired:
            print("Token slut - avbryter (kör om för resume).")
            break
        except gq.PersistedQueryGone:
            pq += 1
            print(f"  meddelande {mid}: persisted query borta - hoppar")
            if pq >= 15:
                print("15 i rad borta - trolig app-deploy. Avbryter.")
                break
            done += 1
            continue
        pq = 0
        if reactors:
            d = json.loads(f.read_text(encoding="utf-8"))
            rec = d.setdefault("messages", {}).setdefault(str(mid), {})
            rec["reactors"] = reactors
            du = d.setdefault("users", {})
            for uid, info in users.items():
                du.setdefault(str(uid), info)
            f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            upgraded += 1
        done += 1
        if done % 10 == 0:
            _progress(done, total, upgraded)
            print(f"  {done}/{total} ({upgraded} kompletterade)")
    _progress(done, total, upgraded)
    print(f"Klart. {upgraded} meddelanden kompletterade av {total}.")


if __name__ == "__main__":
    main()
