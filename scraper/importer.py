"""PoC-importer: skriver in en arkiverad communitys trådar/svar i ett nytt,
PRIVAT test-community i målnätverket via legacy Yammer REST.

Verifierade API-fakta (Microsoft Learn, 2026-06-15):
  - POST messages.json (form-encoded): body, group_id, replied_to_id -> returnerar
    det nya meddelandet (vi får nya id + web_url direkt).
  - INGEN backdatering (created_at/published_at stöds ej) och INGEN redigering via
    API (inget PUT/PATCH). Därför bäddas författare/tid/reaktioner in i TEXTEN, och
    interna länkar substitueras VID skapandet: kronologisk postning löser alla
    bakåtlänkar, övriga pekas mot läs-arkivet (ingen länk blir trasig, inget andra-pass).
  - POST groups.json: name, private, show_in_directory -> 201. DELETE stöds (teardown).

Identitet: den fångade token = en riktig persons webbsession, så inläggen postas
under HENS namn med NUTIDSSTÄMPEL (inte ett neutralt arkiv-konto). PoC:n körs i ett
privat enmans-community -> inga notiser till andra, och hela gruppen är raderbar.

Mentions renderas som text (@Namn) - aldrig riktiga taggar -> ingen avisering.

Kommandon:
  dry-run <gid>   Rendera exakt vad som skulle postas -> data/import/dryrun_<gid>.txt
                  (INGA skrivningar). Granska innan live-körning.
  smoke   <gid>   Skapa privat grupp + 1 trådstart + 1 svar, rapportera. Verifierar
                  skriv-primitiven (201, trådning, returnerade id) innan bulk.
  run     <gid>   Importera hela communityt (resume via id-map på disk).
  teardown <gid>  Radera PoC-communityt och dess id-map (DELETE group).

Kör: uv run python -m scraper.importer dry-run 5673271296
"""

import base64
import html
import json
import random
import re
import sqlite3
import string
import sys
import uuid
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

from . import graphql as gq
from . import yammer
from .yammer import Forbidden, TokenExpired

DB = Path("data/archive.db")
IMPORT_DIR = Path("data/import")
# Läs-arkivets bas-URL: dit pekas interna länkar vi inte (re)postat. Default = publika appen.
ARCHIVE_BASE = "http://ubuntu-ai:8051"

_EMOJI = {
    "like": "👍", "love": "❤️", "laugh": "😄", "celebrate": "🎉",
    "thank": "🙏", "sad": "😢", "angry": "😠", "praise": "👏",
}
_THREAD_ID = re.compile(r"threadId=(\d+)")


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# --- Text-transform: body.rich -> ren text (mentions/taggar/grupper som text) -----

class _PlainRenderer(HTMLParser):
    """Plattar Yammers body.rich till ren text. Mentions -> @Namn (text, inte tagg),
    taggar -> #namn, grupper -> namn, externa länkar -> 'text (url)'. Interna
    tråd-länkar skrivs om mot läs-arkivet."""

    def __init__(self, archive_base: str, known_threads: set[int]) -> None:
        super().__init__(convert_charrefs=True)
        self.archive_base = archive_base
        self.known = known_threads
        self.out: list[str] = []
        self._obj: dict | None = None
        self._obj_depth = 0
        self._href: str | None = None
        self._link_buf: list[str] | None = None

    def _emit(self, s: str) -> None:
        if self._obj is not None:
            self._obj["buf"].append(s)
        elif self._link_buf is not None:
            self._link_buf.append(s)
        else:
            self.out.append(s)

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "span":
            obj = d.get("data-yammer-object")
            if self._obj is None and obj and ":" in obj:
                typ, _, rid = obj.partition(":")
                self._obj = {"type": typ, "id": rid, "buf": []}
                self._obj_depth = 1
            elif self._obj is not None:
                self._obj_depth += 1
            return
        if self._obj is not None:
            return
        if tag == "br":
            self._emit("\n")
            return
        if tag == "a":
            self._href = d.get("href", "")
            self._link_buf = []

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and self._obj is None:
            self._emit("\n")

    def handle_endtag(self, tag):
        if self._obj is not None:
            if tag == "span":
                self._obj_depth -= 1
                if self._obj_depth == 0:
                    self.out.append(self._render_obj(self._obj))
                    self._obj = None
            return
        if tag == "a" and self._link_buf is not None:
            text = "".join(self._link_buf).strip()
            self.out.append(self._render_link(self._href or "", text))
            self._href = None
            self._link_buf = None

    def handle_data(self, data):
        self._emit(data)

    def _render_obj(self, obj: dict) -> str:
        typ, rid = obj["type"], obj["id"]
        text = "".join(obj["buf"]).strip()
        if typ == "user":
            return f"@{text or 'okänd'}"  # text, inte en riktig tagg -> ingen avisering
        if typ == "tag":
            return text if text.startswith("#") else "#" + text
        return text  # group m.m.: bara namnet

    def _render_link(self, href: str, text: str) -> str:
        m = _THREAD_ID.search(href)
        if m and int(m.group(1)) in self.known:
            url = f"{self.archive_base}/arkiv/t/{m.group(1)}"
            return f"{text} ({url})" if text else url
        if not href:
            return text
        return f"{text} ({href})" if text and text != href else href


def _transform_body(rich: str | None, plain: str | None,
                    archive_base: str, known: set[int]) -> str:
    if not rich:
        return (plain or "").strip()
    p = _PlainRenderer(archive_base, known)
    p.feed(rich)
    p.close()
    if p._obj is not None:
        p.out.append(p._render_obj(p._obj))
    text = "".join(p.out)
    text = html.unescape(text)
    # Komprimera överflödiga blankrader.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- Komponering av ett inläggs fulla text (prefix + kropp + sidfot) --------------

_STHLM = ZoneInfo("Europe/Stockholm")


def _fmt_date(created_at: str | None) -> str:
    if not created_at:
        return "okänt datum"
    # Yammer-format: "2019/06/11 14:23:17 +0000" (UTC) -> svensk lokaltid, datum + tid.
    try:
        dt = datetime.strptime(created_at, "%Y/%m/%d %H:%M:%S %z").astimezone(_STHLM)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(created_at)[:16].replace("/", "-")


def _footer(con: sqlite3.Connection, m: sqlite3.Row, seen: int | None,
            is_best: bool, is_verified: bool) -> str:
    parts: list[str] = []
    for r in con.execute("SELECT type, count FROM reactions WHERE message_id=? ORDER BY count DESC",
                         (m["id"],)):
        parts.append(f"{_EMOJI.get(r['type'], '•')} {r['count']}")
    if not parts and m["like_count"]:
        parts.append(f"👍 {m['like_count']}")
    up = con.execute("SELECT count FROM upvotes WHERE message_id=?", (m["id"],)).fetchone()
    if up and up["count"]:
        parts.append(f"⬆ {up['count']}")
    if seen:
        parts.append(f"👁 sett av {seen}")
    if is_best:
        parts.append("✓ markerat som bästa svar")
    elif is_verified:
        parts.append("✓ verifierat svar")
    return "— " + " · ".join(parts) if parts else ""


def _compose(con: sqlite3.Connection, m: sqlite3.Row, *, is_starter: bool,
             seen: int | None, is_best: bool, is_verified: bool,
             archive_base: str, known: set[int],
             reply_to: tuple[str | None, str | None] | None = None) -> str:
    sender = m["sender_name"] or "Okänd användare"
    head = f"Ursprungligen av {sender} · {_fmt_date(m['created_at'])}"
    blocks = [head]
    # Legacy REST plattar trådar (ingen nästling) -> ange svarsmålet i text för de
    # svar som svarade på ett annat svar, så konversationsstrukturen bevaras.
    if reply_to:
        pname = reply_to[0] or "okänd"
        blocks.append(f"↳ Som svar till @{pname} · {_fmt_date(reply_to[1])}")
    if is_starter and m["title"]:
        blocks.append(m["title"].strip())
    body = _transform_body(m["body_rich"], m["body_plain"], archive_base, known)
    if body:
        blocks.append(body)
    foot = _footer(con, m, seen, is_best, is_verified)
    if foot:
        blocks.append(foot)
    return "\n\n".join(blocks)


# --- DB-uthämtning: trådar i kronologisk ordning, meddelanden per tråd ------------

def _known_threads(con: sqlite3.Connection) -> set[int]:
    return {r[0] for r in con.execute("SELECT DISTINCT thread_id FROM messages")}


def _threads_chrono(con: sqlite3.Connection, gid: int) -> list[int]:
    """Tråd-id i communityt, sorterade på trådstartens (= trådens äldsta) tid."""
    rows = con.execute(
        """SELECT thread_id, MIN(created_at) AS t FROM messages
           WHERE group_id=? GROUP BY thread_id ORDER BY t ASC""", (gid,))
    return [r["thread_id"] for r in rows]


def _thread_messages(con: sqlite3.Connection, tid: int) -> list[sqlite3.Row]:
    """Trådens meddelanden i kronologisk ordning (startaren först)."""
    return con.execute(
        """SELECT m.*, u.full_name AS sender_name
           FROM messages m LEFT JOIN users u ON u.id = m.sender_id
           WHERE m.thread_id=? ORDER BY m.created_at ASC, m.id ASC""", (tid,)).fetchall()


def _reply_to(by_id: dict, m: sqlite3.Row, tid: int) -> tuple[str | None, str | None] | None:
    """(förälderns namn, datum) om svaret svarade på ett ANNAT svar (äkta nästling),
    annars None (svar direkt på trådstarten behöver ingen markering - de är platta ändå)."""
    p = m["replied_to_id"]
    if p and p != tid and p in by_id:
        pr = by_id[p]
        return (pr["sender_name"], pr["created_at"])
    return None


def _group_name(con: sqlite3.Connection, gid: int) -> str:
    r = con.execute("SELECT full_name FROM communities WHERE id=?", (gid,)).fetchone()
    return r["full_name"] if r and r["full_name"] else f"community {gid}"


# --- id-map (resume + idempotens) -------------------------------------------------

def _map_path(gid: int) -> Path:
    return IMPORT_DIR / f"idmap_{gid}.json"


def _load_map(gid: int) -> dict:
    p = _map_path(gid)
    if p.exists():
        return json.loads(p.read_text())
    return {"source_group": gid, "new_group": None, "new_group_name": None, "messages": {}}


def _save_map(gid: int, m: dict) -> None:
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    _map_path(gid).write_text(json.dumps(m, ensure_ascii=False, indent=1))


# --- Skriv-operationer ------------------------------------------------------------

def _ensure_group(con: sqlite3.Connection, gid: int, idmap: dict,
                  target: int | None = None) -> int:
    if target:  # användaren har skapat communityt själv -> posta in i det, skapa inget
        if idmap.get("new_group") and idmap["new_group"] != target:
            raise SystemExit(f"id-map pekar redan på grupp {idmap['new_group']}, inte {target}. "
                             f"Kör teardown {gid} eller rensa data/import/idmap_{gid}.json först.")
        idmap["new_group"] = target
        idmap.setdefault("new_group_name", f"(förskapad {target})")
        _save_map(gid, idmap)
        return target
    if idmap.get("new_group"):
        return idmap["new_group"]
    name = f"[Arkiv] {_group_name(con, gid)}"
    print(f"Skapar privat community: {name!r}")
    resp = yammer.post("groups.json", name=name, private="true", show_in_directory="false")
    new_gid = resp.get("id") or (resp.get("group") or {}).get("id")
    if not new_gid:
        raise RuntimeError(f"Kunde inte läsa nytt grupp-id ur svaret: {resp}")
    idmap["new_group"] = new_gid
    idmap["new_group_name"] = name
    idmap["created_by_tool"] = True
    _save_map(gid, idmap)
    print(f"  -> nytt grupp-id {new_gid}")
    return new_gid


def _content_state(text: str) -> str:
    """Bygger Viva-webbklientens DraftJS-`serializedContentState` (JSON-sträng) ur
    ren text. Varje rad blir ett block; mentions/länkar ligger som löptext (inga
    entities) - det räcker för arkiv-import och ger ingen avisering."""
    blocks = []
    for line in text.split("\n"):
        key = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
        blocks.append({"key": key, "text": line, "type": "unstyled", "depth": 0,
                       "inlineStyleRanges": [], "entityRanges": [], "data": {}})
    return json.dumps({"blocks": blocks, "entityMap": {}}, ensure_ascii=False)


def _find_new_message_id(data, exclude_b64: str | None) -> int | None:
    """Letar rekursivt upp det nyskapade meddelandets id i mutationssvaret: första
    base64-strängen som avkodas till en Message-nod och inte är förälder-id:t."""
    found: list[int] = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and o != exclude_b64 and len(o) > 20:
            try:
                obj = json.loads(base64.b64decode(o + "==").decode())
            except Exception:
                return
            if isinstance(obj, dict) and obj.get("_type") == "Message" and "id" in obj:
                found.append(int(obj["id"]))

    walk(data)
    return found[0] if found else None


def _post_reply_gql(text: str, parent_new_id: int, is_second_level: bool) -> int:
    """Postar ett svar via GraphQL-mutationen så nästlingen bevaras. Returnerar
    det nya meddelandets numeriska id."""
    parent_gid = gq.gid("Message", parent_new_id)
    variables = {
        "serializedContentState": _content_state(text),
        "replyToMessageMutationId": parent_gid,
        "isSecondLevelReply": is_second_level,
        "notifiedUserIds": [], "attachmentIds": [],
        "clientMutationId": str(uuid.uuid4()),
        "includeSenderBadges": True, "includeOriginNetworkBadge": True,
        "includeSharePointNewsPost": False, "isModeratorMessage": False,
        "isAnonymousMessage": False, "isPrivateReply": False,
    }
    data = gq.query("PublishReplyMessageClients", variables)
    nid = _find_new_message_id(data, parent_gid)
    if nid is None:
        raise RuntimeError(f"Kunde inte läsa nytt svars-id ur mutationssvaret: {str(data)[:300]}")
    return nid


def _post(body: str, *, group_id: int | None = None, replied_to_id: int | None = None) -> tuple[int, str]:
    data = {"body": body}
    if replied_to_id is not None:
        data["replied_to_id"] = replied_to_id
    elif group_id is not None:
        data["group_id"] = group_id
    resp = yammer.post("messages.json", **data)
    msgs = resp.get("messages") or []
    if not msgs:
        raise RuntimeError(f"messages.json gav inga meddelanden tillbaka: {str(resp)[:200]}")
    nm = msgs[0]
    return nm["id"], nm.get("web_url", "")


# --- Kommandon --------------------------------------------------------------------

def cmd_dry_run(gid: int) -> None:
    con = _db()
    known = _known_threads(con)
    out = IMPORT_DIR / f"dryrun_{gid}.txt"
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    threads = _threads_chrono(con, gid)
    lines: list[str] = [
        f"DRY-RUN: import av '{_group_name(con, gid)}' (källgrupp {gid})",
        f"{len(threads)} trådar. Nytt community skulle bli: '[Arkiv] {_group_name(con, gid)}' (privat).",
        "Inga skrivningar görs. Nedan = exakt brödtext som skulle postas.\n",
    ]
    nmsgs = 0
    for tid in threads:
        msgs = _thread_messages(con, tid)
        by_id = {x["id"]: x for x in msgs}
        meta = con.execute("SELECT seen_by_count, best_reply_id, verified_reply_id "
                           "FROM thread_meta WHERE thread_id=?", (tid,)).fetchone()
        seen = meta["seen_by_count"] if meta else None
        best = meta["best_reply_id"] if meta else None
        verified = meta["verified_reply_id"] if meta else None
        lines.append("=" * 70)
        lines.append(f"TRÅD {tid} ({len(msgs)} inlägg)")
        for i, m in enumerate(msgs):
            nmsgs += 1
            kind = "TRÅDSTART" if i == 0 else f"SVAR (på {m['replied_to_id']})"
            lines.append(f"\n--- {kind} | gammalt id {m['id']} ---")
            lines.append(_compose(con, m, is_starter=(i == 0),
                                  seen=(seen if i == 0 else None),
                                  is_best=(m["id"] == best), is_verified=(m["id"] == verified),
                                  archive_base=ARCHIVE_BASE, known=known,
                                  reply_to=(None if i == 0 else _reply_to(by_id, m, tid))))
    out.write_text("\n".join(lines), encoding="utf-8")
    con.close()
    print(f"Dry-run klar: {len(threads)} trådar, {nmsgs} inlägg.")
    print(f"Granska: {out}")


def cmd_smoke(gid: int, target: int | None = None) -> None:
    con = _db()
    known = _known_threads(con)
    idmap = _load_map(gid)
    print("SMOKE-TEST: 1 trådstart + 1 svar (verifierar skriv-primitiven).")
    new_gid = _ensure_group(con, gid, idmap, target)

    starter_body = ("Smoke-test av arkiv-importen.\n\n"
                    "Detta är ett automatiskt testinlägg i ett privat enmans-community. "
                    "Mentions skulle här renderas som text, t.ex. @Anna Andersson, "
                    "inte som en riktig tagg (så ingen aviseras).")
    sid, surl = _post(starter_body, group_id=new_gid)
    print(f"  trådstart skapad: id {sid}  {surl}")
    idmap["messages"][str(-1)] = {"new_id": sid, "new_url": surl, "smoke": True}

    rid, rurl = _post("Smoke-test: svar i samma tråd (replied_to_id satt).", replied_to_id=sid)
    print(f"  svar skapat: id {rid}  {rurl}")
    idmap["messages"][str(-2)] = {"new_id": rid, "new_url": rurl, "smoke": True}
    _save_map(gid, idmap)
    con.close()

    print("\nSMOKE OK. Verifiera manuellt i Viva:")
    print(f"  - att gruppen är PRIVAT och bara har dig som medlem (inga notiser till andra),")
    print(f"  - att svaret ({rid}) ligger som svar i trådstartens ({sid}) tråd.")
    print(f"  Grupp-id {new_gid}. Kör 'teardown {gid}' för att radera testet.")


def cmd_smoke_nested(gid: int, target: int | None = None) -> None:
    """Verifierar GraphQL-nästlingsvägen: trådstart (REST) + svar nivå 1/2/3 (GraphQL
    PublishReplyMessageClients). Dumpar mutationens råsvar så vi ser id-formen, och
    lägger inläggen i id-mappen (smoke-flaggade) så `clear` städar dem."""
    con = _db()
    idmap = _load_map(gid)
    new_gid = _ensure_group(con, gid, idmap, target)
    con.close()
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)

    print("NÄSTLINGS-SMOKE: trådstart (REST) + svar nivå 1/2/3 (GraphQL).")
    sid, surl = _post("Nästlingstest - trådstart (REST).", group_id=new_gid)
    print(f"  trådstart: id {sid}")
    idmap["messages"]["-10"] = {"new_id": sid, "new_url": surl, "smoke": True}

    # Nivå 1: svar direkt på trådstarten -> isSecondLevelReply=false. Dumpa råsvaret.
    v = {"serializedContentState": _content_state("Svar nivå 1 (direkt på tråden)."),
         "replyToMessageMutationId": gq.gid("Message", sid), "isSecondLevelReply": False,
         "notifiedUserIds": [], "attachmentIds": [], "clientMutationId": str(uuid.uuid4()),
         "includeSenderBadges": True, "includeOriginNetworkBadge": True,
         "includeSharePointNewsPost": False, "isModeratorMessage": False,
         "isAnonymousMessage": False, "isPrivateReply": False}
    data = gq.query("PublishReplyMessageClients", v)
    (IMPORT_DIR / "smoke_nested_response.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1))
    l1 = _find_new_message_id(data, v["replyToMessageMutationId"])
    print(f"  svar nivå 1: id {l1}  (råsvar -> data/import/smoke_nested_response.json)")
    idmap["messages"]["-11"] = {"new_id": l1, "smoke": True}

    # Nivå 2: svar på nivå-1 -> isSecondLevelReply=true.
    l2 = _post_reply_gql("@Nivå1-författare Svar nivå 2 (på ett svar).", l1, True)
    print(f"  svar nivå 2: id {l2}")
    idmap["messages"]["-12"] = {"new_id": l2, "smoke": True}

    # Nivå 3: svar på nivå-2 -> isSecondLevelReply=true (UI plattar visuellt, back-end behåller).
    l3 = _post_reply_gql("@Nivå2-författare Svar nivå 3 (på svar-på-svar).", l2, True)
    print(f"  svar nivå 3: id {l3}")
    idmap["messages"]["-13"] = {"new_id": l3, "smoke": True}

    _save_map(gid, idmap)
    print(f"\nNÄSTLINGS-SMOKE OK. Verifiera i grupp {new_gid}:")
    print("  - att nivå 1/2 nästlar och nivå 3 plattas visuellt men hänger rätt,")
    print("  - kolla data/import/smoke_nested_response.json för id-formen.")
    print(f"  Städa med 'clear {gid}'.")


def cmd_run(gid: int, target: int | None = None) -> None:
    con = _db()
    known = _known_threads(con)
    idmap = _load_map(gid)
    new_gid = _ensure_group(con, gid, idmap, target)
    msgmap: dict = idmap["messages"]
    threads = _threads_chrono(con, gid)
    total = sum(len(_thread_messages(con, t)) for t in threads)
    # Nästlat läge (svar via GraphQL-mutation, äkta nästling) är default; --flat
    # tvingar den gamla platta REST-vägen.
    nested = "--flat" not in sys.argv
    print(f"Importerar {len(threads)} trådar ({total} inlägg) -> grupp {new_gid}. "
          f"{'Nästlat läge (GraphQL-svar).' if nested else 'Platt läge (REST replied_to_id).'}")
    posted = skipped = 0
    try:
        for ti, tid in enumerate(threads, 1):
            msgs = _thread_messages(con, tid)
            by_id = {x["id"]: x for x in msgs}
            meta = con.execute("SELECT seen_by_count, best_reply_id, verified_reply_id "
                               "FROM thread_meta WHERE thread_id=?", (tid,)).fetchone()
            seen = meta["seen_by_count"] if meta else None
            best = meta["best_reply_id"] if meta else None
            verified = meta["verified_reply_id"] if meta else None
            starter_new: int | None = None
            for i, m in enumerate(msgs):
                key = str(m["id"])
                if key in msgmap:  # redan postat (resume)
                    if i == 0:
                        starter_new = msgmap[key]["new_id"]
                    skipped += 1
                    continue
                body = _compose(con, m, is_starter=(i == 0),
                                seen=(seen if i == 0 else None),
                                is_best=(m["id"] == best), is_verified=(m["id"] == verified),
                                archive_base=ARCHIVE_BASE, known=known,
                                reply_to=(None if i == 0 else _reply_to(by_id, m, tid)))
                if i == 0:
                    nid, nurl = _post(body, group_id=new_gid)
                    starter_new = nid
                else:
                    # svara på förälderns NYA id; fall tillbaka till trådstarten.
                    parent_old = str(m["replied_to_id"]) if m["replied_to_id"] else None
                    parent_new = (msgmap.get(parent_old, {}).get("new_id")
                                  if parent_old else None) or starter_new
                    if nested:
                        # GraphQL-mutation: bevarar nästlingen. isSecondLevelReply =
                        # förälder är ett svar (inte trådstarten).
                        is_second = parent_new != starter_new
                        nid = _post_reply_gql(body, parent_new, is_second)
                        nurl = ""
                    else:
                        nid, nurl = _post(body, replied_to_id=parent_new)
                msgmap[key] = {"new_id": nid, "new_url": nurl}
                posted += 1
                if posted % 10 == 0:
                    _save_map(gid, idmap)
                    print(f"  [{ti}/{len(threads)} trådar] {posted} postade, {skipped} hoppade "
                          f"(skriv-intervall {yammer._write_interval:.2f}s)")
    except (TokenExpired, KeyboardInterrupt) as e:
        _save_map(gid, idmap)
        print(f"Avbrutet ({type(e).__name__}): {posted} postade. Kör 'run {gid}' igen för resume.")
        con.close()
        return
    except Forbidden as e:
        _save_map(gid, idmap)
        print(f"Skriv-fel (Forbidden): {e}")
        print("Token saknar troligen skrivbehörighet, eller gruppen tillåter inte postning.")
        con.close()
        return
    _save_map(gid, idmap)
    con.close()
    print(f"KLART. {posted} postade, {skipped} hoppade (redan importerade).")
    print(f"Nytt community-id {new_gid}. id-map: {_map_path(gid)}")


def cmd_mine(_gid: int | None = None) -> None:
    """Listar communities du är medlem i (läsanrop) - för att hitta din test-grupps id."""
    groups = yammer._paginate_groups(mine="true")
    groups.sort(key=lambda g: g.get("id", 0), reverse=True)  # nyast skapade överst
    print(f"{'GRUPP-ID':>16}  {'PRIVAT':<6}  NAMN")
    for g in groups:
        priv = "privat" if g.get("privacy") == "private" else ""
        print(f"{g.get('id'):>16}  {priv:<6}  {g.get('full_name') or g.get('name')}")
    print(f"\n{len(groups)} grupper. Nyast skapade högst upp - din test-grupp lär ligga där.")


def cmd_clear(gid: int) -> None:
    """Raderar alla inlägg vi postat i målgruppen via DELETE, men BEHÅLLER gruppen
    (den skapade du själv). Tömmer id-mappens inlägg så en ny run startar rent."""
    idmap = _load_map(gid)
    msgs = idmap.get("messages", {})
    ids = [(k, e["new_id"]) for k, e in msgs.items() if e.get("new_id")]
    # Radera i omvänd id-ordning (svar/senare före trådstart) för att minska kaskad-404.
    ids.sort(key=lambda kv: kv[1], reverse=True)
    print(f"Raderar {len(ids)} inlägg ur grupp {idmap.get('new_group')} (gruppen behålls).")
    deleted = gone = 0
    for k, mid in ids:
        try:
            yammer.delete(f"messages/{mid}.json")
            deleted += 1
        except Forbidden:
            gone += 1  # redan borta (t.ex. kaskad när en trådstart redan raderats)
        except (TokenExpired, KeyboardInterrupt) as e:
            _save_map(gid, idmap)
            print(f"Avbrutet ({type(e).__name__}): {deleted} raderade. Kör 'clear {gid}' igen.")
            return
        msgs.pop(k, None)
        if (deleted + gone) % 20 == 0:
            _save_map(gid, idmap)
            print(f"  {deleted} raderade, {gone} redan borta")
    _save_map(gid, idmap)
    print(f"KLART. {deleted} raderade, {gone} redan borta. Gruppen {idmap.get('new_group')} kvar, "
          f"id-mappen tömd på inlägg.")


def cmd_teardown(gid: int) -> None:
    idmap = _load_map(gid)
    new_gid = idmap.get("new_group")
    if not new_gid:
        print(f"Ingen importerad grupp för källa {gid} (ingen id-map). Inget att radera.")
        return
    if idmap.get("created_by_tool"):
        print(f"Raderar PoC-community {new_gid} ({idmap.get('new_group_name')}).")
        yammer.delete(f"groups/{new_gid}.json")
        print("  grupp raderad.")
    else:
        print(f"Gruppen {new_gid} skapades av dig, inte av verktyget - raderar den INTE. "
              "Radera den själv i Viva om PoC:n är klar.")
    _map_path(gid).unlink(missing_ok=True)
    print(f"  id-map borttagen ({_map_path(gid)}).")


_COMMANDS = {"dry-run": cmd_dry_run, "smoke": cmd_smoke, "smoke-nested": cmd_smoke_nested,
             "run": cmd_run, "clear": cmd_clear, "teardown": cmd_teardown, "mine": cmd_mine}


def main() -> None:
    if not DB.exists():
        raise SystemExit("Bygg arkivet först (python -m scraper.build).")
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    usage = (
        "Användning:\n"
        "  mine                                lista dina communities + deras id (läsanrop)\n"
        "  dry-run  <källgrupp>                inga skrivningar, renderar till fil\n"
        "  smoke    <källgrupp> <målgrupp>     1 trådstart + 1 svar i din förskapade grupp\n"
        "  smoke-nested <källgrupp> <målgrupp> trådstart + nästlade svar nivå 1/2/3 (GraphQL)\n"
        "  run      <källgrupp> <målgrupp>     importera hela communityt (nästlat via GraphQL; --flat = platt REST)\n"
        "  clear    <källgrupp>                radera alla inlägg vi postat (behåll gruppen)\n"
        "  teardown <källgrupp>                radera id-map (grupp raderar du själv)")
    if not args or args[0] not in _COMMANDS:
        raise SystemExit(usage)
    cmd = args[0]
    if cmd == "mine":
        cmd_mine()
        return
    if len(args) < 2:
        raise SystemExit(usage)
    src = int(args[1])
    target = int(args[2]) if len(args) > 2 else None
    if cmd in ("smoke", "smoke-nested", "run"):
        _COMMANDS[cmd](src, target)
    else:
        _COMMANDS[cmd](src)


if __name__ == "__main__":
    main()
