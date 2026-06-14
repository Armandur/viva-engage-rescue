"""Klient mot Viva Engages moderna GraphQL-API (Apollo persisted queries).

Används av berikningspasset (scraper/enrich.py) för reaktioner + seen-count som
legacy-API:t saknar. POST mot engage.cloud.microsoft/graphql med operationName +
sha256Hash + variables - query-texten skickas aldrig (APQ).

Self-heal token och throttle speglar scraper/yammer.py.

Persisted-query-hasharna nedan fångades 2026-06-09 från webbklienten och ÄNDRAS
vid app-deploy. Går de sönder (PersistedQueryGone) måste nya hashar fångas.
"""

import base64
import json
import time

import requests

from . import config
from .yammer import TokenExpired, _TRANSIENT

GRAPHQL_URL = "https://engage.cloud.microsoft/graphql"

HASHES = {
    "NestedThreadClients": "481f4af76051f85a9dcffaea7a757096dccb380b0d619ec9ec9d6f7ca78ae787",
    "TopLevelRepliesClients": "8dd01fd4a39537be028f8e02e62888ea7faceeff05801a51b4ebd2a27120e3e0",
    "SecondLevelRepliesClients": "3e344863d365d9952d6e0d2c1320665d33190d5760920b9a5a9cd05e767b61b1",
    "FeedUserWallNestedClients": "e822a2b72b8cbfbecb6f28df5e54d74978fd905da21332c3a15a92878d147e2f",
    "GroupSidebarClients": "d02a5254510173dd6b623c01b7c9e42a9c6a922b65eea44519d288498483f468",
    "GroupSidebarAboutClients": "566773dbdf89acd6ae5c00ed58de07dd5e6ee82379370fcc43cc068f5b0ee728",
    "GroupMemberPanelClients": "c5cd3039ffa422c3beaabc8d742f7e537479e19b4bf0a7f5e80acd48e318a03e",
    # Hela reaktörslistan per meddelande (paginerad, after=endCursor). Fångad
    # 2026-06-14. featuredReactions ger bara urvalet (max 8); denna ger alla.
    "MessageReactionsClients": "5263008b3d71ffe625c7037ed657bc41457ab909ebb00a7c4798fada4048731c",
    # Org-övergripande storyline-flöde ("alla"). Noderna är trådar. Paginerar via
    # olderThan = endCursor. Fångad 2026-06-14. Ersätter per-konto-probandet.
    "FeedStorylineAllNestedClients": "47acebf566110a3cc5b5096fa6052a3eca51bf4c77883abb8a263f2764f7d5a1",
}

_MIN_INTERVAL = 1.2
_last_call = 0.0


class PersistedQueryGone(Exception):
    """Hashen känns inte igen längre (app-deploy) - nya hashar måste fångas."""


def gid(typ: str, n) -> str:
    """Bygger en GraphQL-nod-id: base64 av {"_type":typ,"id":"N"}."""
    return base64.b64encode(
        json.dumps({"_type": typ, "id": str(n)}, separators=(",", ":")).encode()
    ).decode()


def gid_decode(b64: str) -> str:
    """Avkodar en GraphQL-nod-id till rå-id-strängen."""
    return json.loads(base64.b64decode(b64 + "==").decode())["id"]


def _throttle() -> None:
    global _last_call
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def query(operation: str, variables: dict) -> dict:
    """Kör en persisted query med självläkande token. Returnerar `data`-objektet.

    Höjer PersistedQueryGone om hashen inte längre känns igen, TokenExpired om
    ingen giltig token dyker upp inom tidsgränsen.
    """
    body = {
        "operationName": operation,
        "variables": variables,
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": HASHES[operation]}},
    }
    payload = json.dumps(body)
    net_fails = 0
    server_fails = 0
    pq_fails = 0
    while True:
        _throttle()
        tok = config.current_token()
        if not tok:
            print("  ingen token satt - väntar (klistra in i panelen)...")
            if config.wait_for_fresh_token(""):
                continue
            raise TokenExpired("ingen token tillgänglig inom tidsgräns")
        try:
            resp = requests.post(
                f"{GRAPHQL_URL}?operationName={operation}",
                headers={"Authorization": f"Bearer {tok}",
                         "Content-Type": "application/json", "Accept": "application/json"},
                data=payload, timeout=60,
            )
        except _TRANSIENT as e:
            net_fails += 1
            if net_fails > 6:
                raise RuntimeError(f"Gav upp efter nätverksfel på {operation}")
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
                raise RuntimeError(f"Gav upp efter {resp.status_code} på {operation}")
            wait = min(2 ** server_fails, 60)
            print(f"  {resp.status_code} serverfel - nytt försök om {wait}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            blob = json.dumps(data["errors"])
            if "PersistedQueryNotFound" in blob:
                # Servern har slängt ut hashen ur APQ-cachen; webbklienten åter-
                # registrerar den löpande. Behandla som transient och vänta in det.
                pq_fails += 1
                if pq_fails > 6:
                    raise PersistedQueryGone(operation)
                wait = min(2 ** pq_fails, 30)
                print(f"  persisted query ej registrerad ({operation}) - "
                      f"väntar {wait}s (håll en Viva-flik öppen så åter-registreras den)")
                time.sleep(wait)
                continue
            if not data.get("data"):
                raise RuntimeError(f"GraphQL-fel ({operation}): {blob[:300]}")
            # Partiella fel men data finns - kör best effort.
        return data["data"]
