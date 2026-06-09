import os

from dotenv import load_dotenv

load_dotenv()

YAMMER_TOKEN = os.getenv("YAMMER_TOKEN", "").strip()
YAMMER_API_BASE = os.getenv("YAMMER_API_BASE", "https://www.yammer.com/api/v1").rstrip("/")


def auth_headers() -> dict[str, str]:
    if not YAMMER_TOKEN:
        raise RuntimeError(
            "YAMMER_TOKEN saknas. Kopiera .env.example till .env och klistra in "
            "din fångade bearer-token."
        )
    return {"Authorization": f"Bearer {YAMMER_TOKEN}"}
