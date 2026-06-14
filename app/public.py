"""Publik, läs-bar arkiv-app - separat ASGI-app från admin-panelen (app.main).

Kör: uvicorn app.public:app --host 0.0.0.0 --port 8051

Serverar ENBART arkivet (/arkiv, read-only). Admin/kontrollpanelen - start/stop
av körningar, token-inklistring, status - ligger kvar i app.main på en annan
port och exponeras alltså inte här. Det gör att arkivet kan publiceras externt
utan att styrningen blir åtkomlig.

Åtkomstskydd (basic auth e.d.) sätts i reverse proxyn framför (Nginx Proxy
Manager). En mer avancerad lösning med riktig användarhantering byggs vid behov.
"""

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.archive import router as archive_router

app = FastAPI(title="Viva Engage-arkiv")
app.include_router(archive_router)


@app.get("/")
def root():
    return RedirectResponse("/arkiv")
