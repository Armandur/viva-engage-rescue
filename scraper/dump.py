"""Råd-dump av alla communities till data/raw/ medan token lever.

Kör:
  uv run python -m scraper.dump            # full dump (resume av oavslutade)
  uv run python -m scraper.dump --update   # inkrementell: hämta bara nya inlägg

Full dump är robust mot token-utgång: skriver sida för sida och markerar varje
grupp som klar (.done), med en .cursor för att återuppta mitt i en grupp.
Tappar token mitt i: fånga ny, uppdatera .env, kör om - färdiga grupper hoppas.

Inkrementell uppdatering (--update) går igenom redan färdiga grupper och hämtar
bara inlägg nyare än gruppens .highwater (högsta sedda message-id), och stannar
så fort den når redan hämtade inlägg. Nytillkomna grupper dumpas fullt.

Ingen tolkning sker här - bara rått JSON sparas. Struktureringen till SQLite
görs separat och dedupar på message-id, så omkörning är ofarlig.
"""

import json
import sys
from pathlib import Path

from . import config, yammer

RAW = Path("data/raw")
UPDATE_PROGRESS = Path("data/update_progress.json")  # nya inlägg under --update


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _highwater(gdir: Path) -> int:
    """Högsta message-id gruppen sett. Faller tillbaka på att räkna ur sidorna."""
    hw = gdir / ".highwater"
    if hw.exists():
        return int(hw.read_text(encoding="utf-8").strip())
    best = 0
    for p in gdir.glob("page_*.json"):
        for m in json.loads(p.read_text(encoding="utf-8")).get("messages", []):
            best = max(best, m["id"])
    return best


def _dump_full(gdir: Path, gid: int, label: str) -> None:
    """Full hämtning av en grupp, med resume via .cursor."""
    cursor_file = gdir / ".cursor"
    existing = sorted(gdir.glob("page_*.json"))
    pageno = len(existing)
    older_than = None
    if cursor_file.exists():
        older_than = int(cursor_file.read_text(encoding="utf-8").strip())
        print(f"{label} - återupptar från sida {pageno + 1}")
    else:
        print(label)
    total = sum(
        len(json.loads(p.read_text(encoding="utf-8")).get("messages", []))
        for p in existing
    )
    max_seen = _highwater(gdir)
    for feed in yammer.iter_group_message_pages(gid, older_than=older_than):
        pageno += 1
        _save(gdir / f"page_{pageno:04d}.json", feed)
        msgs = feed.get("messages", [])
        total += len(msgs)
        if msgs:  # cursor uppdateras först efter lyckad skrivning
            cursor_file.write_text(str(min(m["id"] for m in msgs)), encoding="utf-8")
            max_seen = max(max_seen, max(m["id"] for m in msgs))
        print(f"    sida {pageno}: {len(msgs)} meddelanden (totalt {total})")
    (gdir / ".done").write_text("", encoding="utf-8")
    (gdir / ".highwater").write_text(str(max_seen), encoding="utf-8")
    cursor_file.unlink(missing_ok=True)
    print(f"  klar: {total} meddelanden\n")
    return total


def _dump_incremental(gdir: Path, gid: int, label: str) -> None:
    """Hämta bara inlägg nyare än gruppens highwater."""
    high = _highwater(gdir)
    pageno = len(sorted(gdir.glob("page_*.json")))
    new_total = 0
    max_seen = high
    print(f"{label} - söker nya inlägg")
    for feed in yammer.iter_group_message_pages(gid):  # nyast först
        msgs = feed.get("messages", [])
        ids = [m["id"] for m in msgs]
        new = [i for i in ids if i > high]
        if new:
            pageno += 1
            _save(gdir / f"page_{pageno:04d}.json", feed)
            new_total += len(new)
            max_seen = max(max_seen, max(ids))
        # Stanna när vi nått redan hämtade inlägg (eller tom sida).
        if not msgs or any(i <= high for i in ids):
            break
    if max_seen > high:
        (gdir / ".highwater").write_text(str(max_seen), encoding="utf-8")
    print(f"  {new_total} nya inlägg\n" if new_total else "  inga nya inlägg\n")
    return new_total


def main() -> None:
    update = "--update" in sys.argv
    mode = "inkrementell uppdatering" if update else "full dump"
    print(f"Hämtar grupplista (alla communities i nätverket) - {mode}...")

    try:
        groups = yammer.iter_all_groups()
        _save(RAW / "groups.json", groups)
        sel = config.selected_groups()
        if sel is not None:
            groups = [g for g in groups if g["id"] in sel]
            print(f"  selektiv körning: {len(groups)} av valda communities.\n")
        else:
            print(f"  {len(groups)} grupper.\n")

        total_groups = len(groups)
        new_this_run = 0
        if update:
            UPDATE_PROGRESS.write_text(json.dumps(
                {"new_posts": 0, "checked": 0, "total": total_groups}), encoding="utf-8")
        for i, g in enumerate(groups, 1):
            gid = g["id"]
            name = g.get("full_name") or g.get("name")
            gdir = RAW / "groups" / str(gid)
            label = f"[{i}/{total_groups}] {name} (id {gid})"
            done = (gdir / ".done").exists()

            if done and not update:
                print(f"{label} - redan klar, hoppar")
                continue

            _save(gdir / "group.json", g)
            try:
                if done and update:
                    new_this_run += _dump_incremental(gdir, gid, label) or 0
                else:
                    # Oavslutad grupp, eller helt ny: full hämtning/resume.
                    new_this_run += _dump_full(gdir, gid, label) or 0
            except yammer.Forbidden:
                (gdir / ".skipped").write_text("ingen läsbehörighet\n", encoding="utf-8")
                print("  hoppad: ingen läsbehörighet (privat grupp)\n")
            if update:
                UPDATE_PROGRESS.write_text(json.dumps(
                    {"new_posts": new_this_run, "checked": i, "total": total_groups}),
                    encoding="utf-8")
    except yammer.TokenExpired:
        print(
            "\nTOKEN UTGÅNGEN. Fånga en ny token, uppdatera YAMMER_TOKEN i .env "
            "och kör om - färdiga grupper hoppas (full) eller uppdateras (--update).",
            file=sys.stderr,
        )
        sys.exit(2)

    print("Klart.")


if __name__ == "__main__":
    main()
