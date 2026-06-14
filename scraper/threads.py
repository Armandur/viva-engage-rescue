"""Kompletterar trådar via messages/in_thread - in_group ger bara ett urval.

Kör: uv run python -m scraper.threads

in_group-dumpen är ett trådindex: den listar vilka trådar som finns men ger
inte alla meddelanden i varje tråd (startare och mellanliggande svar saknas
ofta). Den här fasen går igenom alla kända thread_ids och hämtar hela tråden
via in_thread, så arkivet blir komplett. Spara råt; build.py dedupar på id.

Robust som dump.py: .done per tråd, resume mitt i en tråd via .cursor,
token-utgång ger tydligt meddelande, privata/borttagna trådar hoppas.
"""

import json
import sys
from pathlib import Path

from . import config, yammer

RAW = Path("data/raw")
THREADS = RAW / "threads"


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _known_thread_ids() -> list[int]:
    """Alla thread_ids som in_group-dumpen sett (ev. begränsat till valda grupper
    via --groups)."""
    sel = config.selected_groups()
    gdirs = ([RAW / "groups" / str(g) for g in sel] if sel is not None
             else (RAW / "groups").glob("*"))
    ids: set[int] = set()
    for gdir in gdirs:
        for page in gdir.glob("page_*.json"):
            for m in json.loads(page.read_text(encoding="utf-8")).get("messages", []):
                if m.get("thread_id"):
                    ids.add(m["thread_id"])
    return sorted(ids)


def _backfill(tid: int) -> int:
    tdir = THREADS / str(tid)
    cursor_file = tdir / ".cursor"
    existing = sorted(tdir.glob("page_*.json"))
    pageno = len(existing)
    older_than = int(cursor_file.read_text(encoding="utf-8").strip()) if cursor_file.exists() else None
    total = sum(len(json.loads(p.read_text(encoding="utf-8")).get("messages", [])) for p in existing)
    for feed in yammer.iter_thread_pages(tid, older_than=older_than):
        pageno += 1
        _save(tdir / f"page_{pageno:04d}.json", feed)
        msgs = feed.get("messages", [])
        total += len(msgs)
        if msgs:
            cursor_file.write_text(str(min(m["id"] for m in msgs)), encoding="utf-8")
    (tdir / ".done").write_text("", encoding="utf-8")
    cursor_file.unlink(missing_ok=True)
    return total


def main() -> None:
    ids = _known_thread_ids()
    print(f"{len(ids)} kända trådar att komplettera.\n")
    done = skipped = 0
    try:
        for i, tid in enumerate(ids, 1):
            tdir = THREADS / str(tid)
            if (tdir / ".done").exists():
                done += 1
                continue
            try:
                n = _backfill(tid)
                done += 1
                if done % 50 == 0:
                    print(f"  {done}/{len(ids)} trådar klara")
            except yammer.Forbidden:
                # 404/403 sker innan _backfill hunnit skapa trådkatalogen - skapa
                # den så markören kan skrivas (annars FileNotFoundError).
                tdir.mkdir(parents=True, exist_ok=True)
                (tdir / ".skipped").write_text("borttagen/ingen åtkomst\n", encoding="utf-8")
                skipped += 1
    except yammer.TokenExpired:
        print(
            "\nTOKEN UTGÅNGEN. Uppdatera token och kör om - klara trådar hoppas.",
            file=sys.stderr,
        )
        sys.exit(2)
    print(f"Klart. Trådar klara: {done}, hoppade: {skipped}.")


if __name__ == "__main__":
    main()
