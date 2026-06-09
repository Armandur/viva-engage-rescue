"""Råd-dump av alla communities till data/raw/ medan token lever.

Kör: uv run python -m scraper.dump

Robust mot token-utgång: skriver sida för sida och markerar varje grupp
som klar (.done). Körs den om hoppas redan färdiga grupper. Tappar token
mitt i: fånga en ny, uppdatera .env, kör om - bara oavslutade grupper tas om.

Ingen tolkning sker här - bara rått JSON sparas. Struktureringen till SQLite
görs separat efteråt (behöver ingen token).
"""

import json
import sys
from pathlib import Path

from . import yammer

RAW = Path("data/raw")


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    print("Hämtar grupplista (alla communities i nätverket)...")
    groups = yammer.iter_all_groups()
    _save(RAW / "groups.json", groups)
    print(f"  {len(groups)} grupper.\n")

    try:
        for i, g in enumerate(groups, 1):
            gid = g["id"]
            name = g.get("full_name") or g.get("name")
            gdir = RAW / "groups" / str(gid)
            done_marker = gdir / ".done"
            if done_marker.exists():
                print(f"[{i}/{len(groups)}] {name} - redan klar, hoppar")
                continue

            _save(gdir / "group.json", g)
            cursor_file = gdir / ".cursor"
            # Återuppta mitt i en grupp om en tidigare körning avbröts.
            existing = sorted(gdir.glob("page_*.json"))
            pageno = len(existing)
            older_than = None
            if cursor_file.exists():
                older_than = int(cursor_file.read_text(encoding="utf-8").strip())
                print(f"[{i}/{len(groups)}] {name} (id {gid}) - återupptar från sida {pageno + 1}")
            else:
                print(f"[{i}/{len(groups)}] {name} (id {gid})")
            total = sum(
                len(json.loads(p.read_text(encoding="utf-8")).get("messages", []))
                for p in existing
            )
            try:
                for feed in yammer.iter_group_message_pages(gid, older_than=older_than):
                    pageno += 1
                    _save(gdir / f"page_{pageno:04d}.json", feed)
                    msgs = feed.get("messages", [])
                    total += len(msgs)
                    if msgs:  # uppdatera cursor först efter lyckad skrivning
                        cursor_file.write_text(str(min(m["id"] for m in msgs)),
                                               encoding="utf-8")
                    print(f"    sida {pageno}: {len(msgs)} meddelanden (totalt {total})")
            except yammer.Forbidden:
                (gdir / ".skipped").write_text("ingen läsbehörighet\n", encoding="utf-8")
                print("  hoppad: ingen läsbehörighet (privat grupp)\n")
                continue
            done_marker.write_text("", encoding="utf-8")
            cursor_file.unlink(missing_ok=True)
            print(f"  klar: {total} meddelanden\n")
    except yammer.TokenExpired:
        print(
            "\nTOKEN UTGÅNGEN. Fånga en ny från en inloggad session, uppdatera "
            "YAMMER_TOKEN i .env och kör om - färdiga grupper hoppas över.",
            file=sys.stderr,
        )
        sys.exit(2)

    print("Alla grupper klara. Råd-dump i data/raw/.")


if __name__ == "__main__":
    main()
