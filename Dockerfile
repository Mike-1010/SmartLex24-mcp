# Usiamo l'immagine ufficiale di Playwright: ha già Chromium e tutte le
# librerie di sistema necessarie pre-installate, quindi evitiamo del tutto
# il comando "playwright install --with-deps" che su Render fallisce
# (richiederebbe permessi di root non disponibili in fase di build).
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "server.py"]
