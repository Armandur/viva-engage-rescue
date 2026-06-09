"""Auth-spike: verifierar att den fångade tokenen funkar mot legacy-API:t.

Kör: uv run python -m scraper.spike

Gör tre saker:
  1. Bekräftar token via /users/current (vem är du, vilket nätverk).
  2. Listar de communities/grupper du är medlem i via /groups.
  3. Visar att paginering fungerar genom att hämta första sidan av
     meddelanden i den första gruppen.

Inget sparas - detta är bara ett funktionstest innan vi bygger skrapern.
"""

import sys

import requests

from .config import YAMMER_API_BASE, auth_headers


def _get(path: str, **params):
    url = f"{YAMMER_API_BASE}/{path.lstrip('/')}"
    resp = requests.get(url, headers=auth_headers(), params=params, timeout=30)
    if resp.status_code == 401:
        sys.exit(
            "401 Unauthorized - tokenen är ogiltig eller har gått ut. "
            "Fånga en ny från en inloggad session och uppdatera .env."
        )
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "?")
        sys.exit(f"429 Rate limited - vänta {retry}s och försök igen.")
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    print("== 1. Verifierar token (/users/current) ==")
    me = _get("users/current.json")
    print(f"  Inloggad som: {me.get('full_name')} <{me.get('email')}>")
    network = me.get("network_name") or me.get("network_id")
    print(f"  Nätverk: {network}")

    print("\n== 2. Communities/grupper du är medlem i (/groups) ==")
    groups = _get("groups.json", mine="true")
    if isinstance(groups, dict):  # vissa svar wrappar i {"groups": [...]}
        groups = groups.get("groups", [])
    print(f"  Antal: {len(groups)}")
    for g in groups[:20]:
        print(f"    [{g.get('id')}] {g.get('full_name') or g.get('name')}")
    if len(groups) > 20:
        print(f"    ... och {len(groups) - 20} till")

    if not groups:
        print("\nInga grupper hittades - kan inte testa meddelande-hämtning.")
        return

    first = groups[0]
    gid = first.get("id")
    print(f"\n== 3. Första sidan meddelanden i grupp {gid} "
          f"({first.get('full_name') or first.get('name')}) ==")
    feed = _get(f"messages/in_group/{gid}.json")
    messages = feed.get("messages", []) if isinstance(feed, dict) else []
    print(f"  Meddelanden på första sidan: {len(messages)}")
    if messages:
        m = messages[0]
        body = (m.get("body", {}) or {}).get("plain", "")
        print(f"  Senaste: {body[:120]!r}")
        print(f"  older_available: {feed.get('meta', {}).get('older_available')}")

    print("\nOK - tokenen funkar och API:t svarar. Klart att bygga skrapern.")


if __name__ == "__main__":
    main()
