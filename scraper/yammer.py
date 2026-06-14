"""Tunn klient mot legacy Yammer REST API med throttle och 429-backoff."""

import time

import requests

from . import config

# Legacy-API:t tål grovt 10 req/10s. 1.2s mellan anrop ger marginal.
_MIN_INTERVAL = 1.2
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


class TokenExpired(Exception):
    """Tokenen är ogiltig/utgången - fånga en ny och kör om."""


class Forbidden(Exception):
    """Ingen läsbehörighet (t.ex. privat grupp utan medlemskap) - hoppa."""


# Transienta nätverksfel som ska försökas igen i stället för att krascha dumpen.
_TRANSIENT = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _request(url: str, **params) -> requests.Response:
    """Gör en GET med självläkande token: läser token färskt per anrop och
    väntar in en ny (inklistrad i panelen) vid 401 i stället för att krascha.
    Backar av vid 429/nätverksfel."""
    net_fails = 0
    server_fails = 0
    while True:
        _throttle()
        tok = config.current_token()
        if not tok:
            print("  ingen token satt - väntar (klistra in i panelen)...")
            if config.wait_for_fresh_token(""):
                continue
            raise TokenExpired("ingen token tillgänglig inom tidsgräns")
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {tok}"},
                                params=params, timeout=60)
        except _TRANSIENT as e:
            net_fails += 1
            if net_fails > 6:
                raise RuntimeError(f"Gav upp efter nätverksfel på {url}")
            wait = min(2 ** net_fails, 30)
            print(f"  nätverksfel ({type(e).__name__}) - nytt försök om {wait}s")
            time.sleep(wait)
            continue
        net_fails = 0
        if resp.status_code == 401:
            print("  token utgången - väntar på ny (klistra in i panelen)...")
            if config.wait_for_fresh_token(tok):
                print("  ny token mottagen - fortsätter")
                continue
            raise TokenExpired("401, ingen ny token inom tidsgräns")
        if resp.status_code == 429:
            retry = int(resp.headers.get("Retry-After", 10))
            print(f"  429 rate limit - väntar {retry}s")
            time.sleep(retry)
            continue
        if resp.status_code in (500, 502, 503, 504):
            server_fails += 1
            if server_fails > 8:
                raise RuntimeError(f"Gav upp efter {resp.status_code} på {url}")
            wait = min(2 ** server_fails, 60)
            print(f"  {resp.status_code} serverfel - nytt försök om {wait}s")
            time.sleep(wait)
            continue
        return resp


def get(path: str, **params) -> dict | list:
    """GET mot API:t med självläkande token. Höjer Forbidden vid 403/404."""
    resp = _request(f"{config.YAMMER_API_BASE}/{path.lstrip('/')}", **params)
    if resp.status_code in (403, 404):
        raise Forbidden(f"{resp.status_code} på {path}")
    resp.raise_for_status()
    return resp.json()


def _paginate_groups(**extra) -> list[dict]:
    groups: list[dict] = []
    page = 1
    while True:
        batch = get("groups.json", page=page, **extra)
        if isinstance(batch, dict):
            batch = batch.get("groups", [])
        if not batch:
            break
        groups.extend(batch)
        if len(batch) < 50:  # API ger 50 per sida
            break
        page += 1
    return groups


def iter_all_groups() -> list[dict]:
    """Alla communities i nätverket (publika + de privata man är med i).

    `groups.json` listar nätverkets publika grupper. Unioneras med `mine=true`
    för att fånga privata grupper man är medlem i som inte ligger i den listan.
    """
    by_id: dict[int, dict] = {}
    for g in _paginate_groups():
        by_id[g["id"]] = g
    for g in _paginate_groups(mine="true"):
        by_id.setdefault(g["id"], g)
    return list(by_id.values())


def download(url: str, dest) -> str:
    """Laddar ner en fil (bilaga) till dest. Självläkande token. Returnerar content-type."""
    resp = _request(url)
    if resp.status_code in (403, 404):
        raise Forbidden(f"{resp.status_code} vid nedladdning {url}")
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return resp.headers.get("content-type", "")


_PAGE_LIMIT = 20


def _iter_message_pages(path: str, older_than: int | None = None):
    """Yieldar råa feed-sidor (äldre och äldre) tills en ofullständig sida nås.

    OBS: Yammers `meta.older_available` är opålitlig - den kan vara False trots
    att äldre meddelanden finns (observerat på in_thread), vilket trunkerar
    trådar (tappar de äldsta meddelandena, inkl. startaren). Därför paginerar vi
    på sid-fullhet i stället: full sida (== limit) -> hämta äldre; ofullständig
    sida -> klart. No-progress-skydd mot oändlig loop.
    """
    while True:
        params = {"limit": _PAGE_LIMIT}
        if older_than is not None:
            params["older_than"] = older_than
        feed = get(path, **params)
        messages = feed.get("messages", []) if isinstance(feed, dict) else []
        yield feed
        if len(messages) < _PAGE_LIMIT:
            break
        new_older = min(m["id"] for m in messages)
        if older_than is not None and new_older >= older_than:
            break  # ingen progress
        older_than = new_older


def iter_group_message_pages(group_id: int, older_than: int | None = None):
    """Generator: yieldar råa feed-sidor för en grupp (in_group), äldre och äldre.
    `older_than` låter en avbruten körning återuppta mitt i en grupp."""
    yield from _iter_message_pages(f"messages/in_group/{group_id}.json", older_than)


def iter_thread_pages(thread_id: int, older_than: int | None = None):
    """Generator: yieldar råa feed-sidor för en hel tråd (in_thread)."""
    yield from _iter_message_pages(f"messages/in_thread/{thread_id}.json", older_than)
