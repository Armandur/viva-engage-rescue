import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

YAMMER_API_BASE = os.getenv("YAMMER_API_BASE", "https://www.yammer.com/api/v1").rstrip("/")


def selected_groups() -> set[int] | None:
    """Grupp-id ur '--groups id1,id2' (eller '--groups=...') i argv, annars None.

    None betyder 'alla communities'. Används för selektiv körning av ett pass mot
    bara vissa communities (omkörning/test)."""
    args = sys.argv
    raw = None
    for i, a in enumerate(args):
        if a == "--groups" and i + 1 < len(args):
            raw = args[i + 1]
            break
        if a.startswith("--groups="):
            raw = a.split("=", 1)[1]
            break
    if raw is None:
        return None
    return {int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}


def excluded_groups() -> set[int]:
    """Grupp-id som ALLTID ska hoppas (motsatsen till --groups): '--exclude id1,id2'
    i argv eller EXCLUDE_GROUPS i miljön. Används för att hålla våra egna test-/arkiv-
    grupper (skapade i nätverket av importern) utanför den vanliga exporten."""
    args = sys.argv
    raw = None
    for i, a in enumerate(args):
        if a == "--exclude" and i + 1 < len(args):
            raw = args[i + 1]
            break
        if a.startswith("--exclude="):
            raw = a.split("=", 1)[1]
            break
    if raw is None:
        raw = os.getenv("EXCLUDE_GROUPS", "")
    return {int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}


def current_token() -> str:
    """Läser token färskt ur .env varje gång, så en ny token (inklistrad i
    panelen) plockas upp av en pågående körning utan omstart."""
    load_dotenv(override=True)
    return os.getenv("YAMMER_TOKEN", "").strip()


def wait_for_fresh_token(stale: str = "", timeout: int = 1800, interval: int = 15) -> bool:
    """Väntar tills .env har en annan token än `stale` (eller timeout).
    Returnerar True om en ny token dök upp."""
    waited = 0
    while waited < timeout:
        time.sleep(interval)
        waited += interval
        tok = current_token()
        if tok and tok != stale:
            return True
    return False


def auth_headers() -> dict[str, str]:
    tok = current_token()
    if not tok:
        raise RuntimeError(
            "YAMMER_TOKEN saknas. Kopiera .env.example till .env och klistra in "
            "din fångade bearer-token (eller spara den via kontrollpanelen)."
        )
    return {"Authorization": f"Bearer {tok}"}
