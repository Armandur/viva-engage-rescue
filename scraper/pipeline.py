"""Kör hela insamlingskedjan i serie - ett "kör och passa inte"-pass.

Kör: uv run python -m scraper.pipeline   (eller panelknappen "Kör hela kedjan")

Stegen körs i beroendeordning som delprocesser. Varje pass är resumebart och
idempotent (dump hoppar klara grupper, enrich/download hoppar klart, build är
full ombyggnad), så hela kedjan är säker att köra om - klart arbete hoppas.

Token: varje pass självläker och väntar in en färsk token (upp till 30 min via
userscriptet). Håll en Viva-flik öppen så matas token automatiskt och kedjan
rullar utan tillsyn. Tar token slut (pass avslutar med kod 2) stoppas kedjan så
att du kan fixa token och köra om - den fortsätter där den var.

`build` läggs mellan stegen eftersom flera pass läser arkivdatabasen (enrich/
community_info/users-avatarer läser DB, storylines backfillar nya trådar som
build måste plocka upp innan enrich).
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config

ROOT = Path(__file__).resolve().parent.parent
PROGRESS = ROOT / "data" / "pipeline_progress.json"

_current: subprocess.Popen | None = None


def _terminate(signum, frame) -> None:
    """Panelens Stopp skickar SIGTERM hit - döda även aktivt delsteg, annars
    blir det föräldralöst och fortsätter köra."""
    if _current and _current.poll() is None:
        _current.terminate()
    sys.exit(143)

# (visningsnamn, modul-argv, loggfil i data/)
STEPS = [
    ("dump", ["scraper.dump"], "dump.log"),
    ("threads", ["scraper.threads"], "threads.log"),
    ("build", ["scraper.build"], "build.log"),
    ("storylines", ["scraper.storylines"], "storylines.log"),
    ("build", ["scraper.build"], "build.log"),
    ("enrich", ["scraper.enrich"], "enrich.log"),
    ("reactors", ["scraper.reactors"], "reactors.log"),
    ("community_info", ["scraper.community_info"], "community_info.log"),
    ("users", ["scraper.users"], "users.log"),
    ("download", ["scraper.download"], "download.log"),
    ("build", ["scraper.build"], "build.log"),
]


def _write(step: int, name: str, status: str) -> None:
    PROGRESS.write_text(json.dumps({
        "step": step, "name": name, "total": len(STEPS), "status": status,
        "updated": time.time(),
    }), encoding="utf-8")


def main() -> None:
    global _current
    signal.signal(signal.SIGTERM, _terminate)
    sel = config.selected_groups()
    groups = ["--groups", ",".join(str(g) for g in sorted(sel))] if sel else []
    total = len(STEPS)
    print(f"Startar hela kedjan ({total} steg)"
          + (f" begränsad till {len(sel)} communities" if sel else "") + ".", flush=True)
    for i, (name, argv, logname) in enumerate(STEPS, 1):
        cmd = [sys.executable, "-m", *argv]
        if name != "build":  # build är alltid full ombyggnad, tar inte --groups
            cmd += groups
        print(f"=== steg {i}/{total}: {name} ===", flush=True)
        _write(i, name, "kör")
        with (ROOT / "data" / logname).open("w", encoding="utf-8") as log:
            _current = subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            rc = _current.wait()
            _current = None
        if rc != 0:
            if rc == 2:
                why = ("token slut - öppna en Viva-flik och kör om kedjan, "
                       "den fortsätter där den var")
            else:
                why = f"oväntat fel - se data/{logname}, åtgärda och kör om kedjan"
            print(f"steg {i}/{total} ({name}) avbröts (kod {rc}) - stoppar kedjan. "
                  f"{why}.", flush=True)
            _write(i, name, f"stoppad (kod {rc})")
            sys.exit(rc)
        print(f"steg {i}/{total} ({name}) klart.", flush=True)
    _write(total, "klar", "klar")
    print("Hela kedjan klar.", flush=True)


if __name__ == "__main__":
    main()
