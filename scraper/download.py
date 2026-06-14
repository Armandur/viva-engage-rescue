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

import requests

from . import config, yammer

RAW = Path("data/raw")
ATT = Path("data/attachments")
THUMBS = Path("data/thumbnails")  # ymodule-länkkortens förhandsbilder
DENIED = Path("data/attachments_denied.json")  # nekade bilagor, visas i arkiv-UI
PROGRESS = Path("data/download_progress.json")  # progress för panelen

# Bilagetyper som har faktiska filbytes att ladda ner.
_FILE_TYPES = {"image", "file", "video"}
_CT_EXT = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
           "image/jpeg": ".jpg", "image/jpg": ".jpg"}


def _safe(name: str) -> str:
    name = re.sub(r"[^\w.\-]+", "_", name or "fil")
    return name[:120]


def _collect() -> dict[str, dict]:
    """Unika bilagor (file_id -> {url, name, type}) över alla dumpade sidor
    (både in_group- och trådbackfill-sidor). Ev. begränsat till valda grupper."""
    sel = config.selected_groups()
    found: dict[str, dict] = {}
    pages = list(RAW.glob("groups/**/page_*.json")) + list(RAW.glob("threads/**/page_*.json"))
    for page in pages:
        data = json.loads(page.read_text(encoding="utf-8"))
        for msg in data.get("messages", []):
            if sel is not None and msg.get("group_id") not in sel:
                continue
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
                    "web_url": a.get("web_url"),
                    "post_web_url": msg.get("web_url"),
                    "message_id": msg.get("id"),
                    "thread_id": msg.get("thread_id"),
                    "group_id": msg.get("group_id"),
                }
    return found


def _collect_thumbs() -> dict[str, str]:
    """Unika ymodule-förhandsbilder (attachment-id -> thumbnail_url) över alla
    sidor. Bilden ligger på Yammers CDN och försvinner vid nedstängning, så vi
    laddar ner den lokalt för att bevara länkkortens miniatyr."""
    sel = config.selected_groups()
    found: dict[str, str] = {}
    pages = list(RAW.glob("groups/**/page_*.json")) + list(RAW.glob("threads/**/page_*.json"))
    for page in pages:
        data = json.loads(page.read_text(encoding="utf-8"))
        for msg in data.get("messages", []):
            if sel is not None and msg.get("group_id") not in sel:
                continue
            for a in msg.get("attachments", []):
                if a.get("type") == "ymodule" and a.get("thumbnail_url") and a.get("id"):
                    found[str(a["id"])] = a["thumbnail_url"]
    return found


def _fetch_thumb(url: str, dest_noext: Path) -> bool:
    """Hämtar en länkkortsminiatyr direkt (publik CDN, ingen auth). Kort timeout
    och inga omförsök - bilderna hämtas via Yammers proxy från externa sajter och
    är flakiga; en död får inte hänga passet. True om en bild sparades."""
    try:
        r = requests.get(url, timeout=12)
    except requests.RequestException:
        return False
    ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if r.status_code != 200 or not ct.startswith("image"):
        return False
    dest_noext.with_suffix(_CT_EXT.get(ct, ".jpg")).write_bytes(r.content)
    return True


def _denied_entry(fid: str, info: dict) -> dict:
    return {
        "file_id": fid, "name": info["name"], "type": info["type"],
        "file_url": info.get("web_url") or info["url"], "download_url": info["url"],
        "post_web_url": info.get("post_web_url"),
        "message_id": info.get("message_id"), "thread_id": info.get("thread_id"),
        "group_id": info.get("group_id"),
    }


def _write_progress(done: int, total: int, downloaded: int, skipped: int, denied: int) -> None:
    PROGRESS.write_text(json.dumps({
        "done": done, "total": total, "downloaded": downloaded,
        "skipped": skipped, "denied": denied,
    }), encoding="utf-8")


def main() -> None:
    items = _collect()
    thumbs = _collect_thumbs()
    total = len(items) + len(thumbs)
    print(f"{len(items)} unika filbilagor + {len(thumbs)} länkkortsminiatyrer i dumpen.")
    done = skipped = failed = 0
    denied: list[dict] = []
    _write_progress(0, total, 0, 0, 0)
    try:
        for fid, info in items.items():
            dest = ATT / f"{fid}_{_safe(info['name'])}"
            if dest.exists():
                skipped += 1
            else:
                try:
                    yammer.download(info["url"], dest)
                    done += 1
                    if done % 20 == 0:
                        print(f"  laddat {done} filer...")
                except yammer.Forbidden:
                    failed += 1
                    denied.append(_denied_entry(fid, info))
                    print(f"  nekad åtkomst: {info['name']}")
            if (done + skipped + failed) % 10 == 0:
                _write_progress(done + skipped + failed, total, done, skipped, failed)
        THUMBS.mkdir(parents=True, exist_ok=True)
        for tid, url in thumbs.items():
            if next(THUMBS.glob(f"{tid}.*"), None):
                skipped += 1
            elif _fetch_thumb(url, THUMBS / tid):
                done += 1
            else:
                failed += 1
            if (done + skipped + failed) % 10 == 0:
                _write_progress(done + skipped + failed, total, done, skipped, failed)
    except yammer.TokenExpired:
        DENIED.write_text(json.dumps(denied, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_progress(done + skipped + failed, total, done, skipped, failed)
        print(
            "\nTOKEN UTGÅNGEN. Uppdatera YAMMER_TOKEN och kör om - "
            "redan nedladdade filer hoppas.",
            file=sys.stderr,
        )
        sys.exit(2)
    DENIED.write_text(json.dumps(denied, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_progress(total, total, done, skipped, failed)
    print(f"Klart. Nya: {done}, redan fanns: {skipped}, nekade: {failed}.")


if __name__ == "__main__":
    main()
