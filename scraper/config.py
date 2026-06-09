import os
import time

from dotenv import load_dotenv

load_dotenv()

YAMMER_API_BASE = os.getenv("YAMMER_API_BASE", "https://www.yammer.com/api/v1").rstrip("/")


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
