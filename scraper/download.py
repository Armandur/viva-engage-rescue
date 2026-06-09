"""Laddar ner alla filbilagor från råd-dumpen till data/attachments/.

Kör: uv run python -m scraper.download

Läser de dumpade page-filerna, plockar varje bilaga med en download_url
(bilder, filer, videor - inte rena länk-embeds av typen ymodule) och hämtar
bytesen. Idempotent: redan nedladdade filer hoppas. Hanterar token-utgång som
dump.py - fånga ny token och kör om.
"""

import json
import re
import sys
from pathlib import Path

from . import yammer

RAW = Path("data/raw")
ATT = Path("data/attachments")

# Bilagetyper som har faktiska filbytes att ladda ner.
_FILE_TYPES = {"image", "file", "video"}


def _safe(name: str) -> str:
    name = re.sub(r"[^\w.\-]+", "_", name or "fil")
    return name[:120]


def _collect() -> dict[str, dict]:
    """Unika bilagor (file_id -> {url, name, type}) över alla dumpade sidor."""
    found: dict[str, dict] = {}
    for page in RAW.glob("groups/**/page_*.json"):
        data = json.loads(page.read_text(encoding="utf-8"))
        for msg in data.get("messages", []):
            for a in msg.get("attachments", []):
                if a.get("type") not in _FILE_TYPES:
                    continue
                url = a.get("download_url") or a.get("url")
                if not url:
                    continue
                fid = str(a.get("id") or a.get("uuid"))
                found[fid] = {
                    "url": url,
                    "name": a.get("original_name") or a.get("name") or fid,
                    "type": a.get("type"),
                }
    return found


def main() -> None:
    items = _collect()
    print(f"{len(items)} unika filbilagor i dumpen.")
    done = skipped = failed = 0
    try:
        for fid, info in items.items():
            dest = ATT / f"{fid}_{_safe(info['name'])}"
            if dest.exists():
                skipped += 1
                continue
            try:
                yammer.download(info["url"], dest)
                done += 1
                if done % 20 == 0:
                    print(f"  laddat {done} filer...")
            except yammer.Forbidden:
                failed += 1
                print(f"  hoppad (åtkomst nekad): {info['name']}")
    except yammer.TokenExpired:
        print(
            "\nTOKEN UTGÅNGEN. Uppdatera YAMMER_TOKEN och kör om - "
            "redan nedladdade filer hoppas.",
            file=sys.stderr,
        )
        sys.exit(2)
    print(f"Klart. Nya: {done}, redan fanns: {skipped}, misslyckade: {failed}.")


if __name__ == "__main__":
    main()
