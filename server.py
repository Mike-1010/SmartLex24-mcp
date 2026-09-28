"""Connettore MCP per SmartLex24 (Il Sole 24 Ore) — richiede credenziali di
abbonamento (username/password), configurate come variabili d'ambiente sul
server, mai nel codice.

Il sito è una SPA che, dopo un login HTML classico (form POST a
du.ilsole24ore.com), genera lato client un token di sessione opaco usato in
ogni chiamata REST successiva (endpoint PullSearch3 per la ricerca). Questo
token non nasce da una singola risposta HTTP leggibile: per ottenerlo usiamo
un vero browser headless (Playwright) che fa login e lascia partire le
chiamate della pagina, intercettando la prima che porta il token nel payload.
Una volta ottenuto, lo riusiamo direttamente via httpx per le ricerche
successive, senza riaprire il browser ogni volta (finché resta valido).
"""
import argparse
import asyncio
import os
import sys
import time
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

import config

INSTRUCTIONS = """\
Accesso a SmartLex24 (Il Sole 24 Ore) con le credenziali di abbonamento
configurate sul server. Copre giurisprudenza, legge e prassi, approfondimenti
professionali, secondo quanto incluso nell'abbonamento collegato.

Usa `cerca_smartlex24` per le ricerche testuali.

ATTENZIONE - connettore sperimentale, in fase di prima verifica: la
struttura dei risultati non è stata ancora interamente mappata (i campi
esatti di ogni documento vanno controllati sui risultati reali). Il primo
login di una sessione è lento (apre un vero browser per autenticarsi): le
ricerche successive sono più rapide perché riusano la sessione già ottenuta.
"""

mcp = FastMCP(
    "smartlex24",
    instructions=INSTRUCTIONS,
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("PORT", "8000")),
    streamable_http_path=os.getenv("MCP_PATH", "/mcp"),
)

_session_lock = asyncio.Lock()
_session_token: Optional[str] = None
_session_cookies: Optional[list] = None
_session_ready_at = 0.0


class LoginError(Exception):
    """Il login non è andato a buon fine, o non è stato possibile catturare
    il token di sessione dopo un login apparentemente riuscito."""


async def _login_and_capture_token() -> tuple[str, list]:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

    if not config.SMARTLEX24_USERNAME or not config.SMARTLEX24_PASSWORD:
        raise LoginError(
            "Credenziali non configurate: imposta le variabili d'ambiente "
            "SMARTLEX24_USERNAME e SMARTLEX24_PASSWORD sul server."
        )

    captured_token: Optional[str] = None

    def _on_request(request):
        nonlocal captured_token
        if captured_token or request.method != "POST":
            return
        if "dwa.ilsole24ore.com/dir/api/" not in request.url:
            return
        try:
            data = request.post_data_json
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("token"):
            captured_token = data["token"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        page.on("request", _on_request)

        try:
            await page.goto(
                config.LOGIN_PAGE_URL,
                timeout=config.NAV_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
            await page.fill(config.LOGIN_USERNAME_SELECTOR, config.SMARTLEX24_USERNAME)
            await page.fill(config.LOGIN_PASSWORD_SELECTOR, config.SMARTLEX24_PASSWORD)

            # Il banner cookie (OneTrust) copre il bottone "Accedi" e ne
            # blocca il click: va chiuso prima. Non è detto compaia sempre
            # (se il browser Playwright avesse già un consenso salvato, ma
            # essendo un context nuovo ad ogni login capita raramente), quindi
            # tentiamo con timeout brevi senza far fallire il login se assente.
            for selector in (config.COOKIE_REJECT_SELECTOR, config.COOKIE_ACCEPT_SELECTOR):
                try:
                    btn = page.locator(selector)
                    if await btn.is_visible(timeout=config.COOKIE_BANNER_WAIT_MS):
                        await btn.click()
                        break
                except PlaywrightTimeoutError:
                    continue

            await page.click(config.LOGIN_SUBMIT_SELECTOR)

            try:
                await page.wait_for_load_state("networkidle", timeout=config.NAV_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass  # la SPA può tenere connessioni aperte: non è di per sé un errore

            # Controllo esplicito di un eventuale messaggio di login rifiutato
            # (credenziali sbagliate), per dare un errore chiaro invece di un
            # generico timeout nella cattura del token.
            try:
                error_el = page.locator(config.LOGIN_ERROR_SELECTOR)
                if await error_el.is_visible(timeout=2000):
                    msg = (await error_el.inner_text()).strip()
                    if msg:
                        raise LoginError(f"Login rifiutato da SmartLex24: {msg}")
            except PlaywrightTimeoutError:
                pass

            if not captured_token:
                await page.wait_for_timeout(config.LOGIN_WAIT_MS)
        except PlaywrightTimeoutError as e:
            raise LoginError(f"Timeout durante il login: {e}") from e
        finally:
            cookies = await context.cookies()
            await browser.close()

    if not captured_token:
        raise LoginError(
            "Login apparentemente riuscito ma non è stato possibile catturare "
            "il token di sessione dalle chiamate della pagina (il sito potrebbe "
            "aver cambiato il proprio funzionamento interno: da riverificare "
            "con DevTools)."
        )

    return captured_token, cookies


async def ensure_session() -> str:
    global _session_token, _session_cookies, _session_ready_at
    now = time.monotonic()
    if _session_token and (now - _session_ready_at) < config.SESSION_TTL:
        return _session_token
    async with _session_lock:
        now = time.monotonic()
        if _session_token and (now - _session_ready_at) < config.SESSION_TTL:
            return _session_token
        token, cookies = await _login_and_capture_token()
        _session_token = token
        _session_cookies = cookies
        _session_ready_at = time.monotonic()
        return _session_token


def _cookie_header(cookies: Optional[list]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in (cookies or []))


async def _search(query: str, n: int) -> dict:
    try:
        token = await ensure_session()
    except LoginError as e:
        return {"errore": str(e)}

    payload = {
        "parameters": {
            "proustFilter": True,
            "excludeNotSearchablePackets": True,
            "facet": "tipologia",
            "facetMinCount": 1,
            "queryText": query,
            "rows": max(1, min(n, config.MAX_ROWS)),
        },
        "token": token,
    }
    headers = {"Content-Type": "application/json"}
    if _session_cookies:
        headers["Cookie"] = _cookie_header(_session_cookies)

    async with httpx.AsyncClient(timeout=config.TIMEOUT) as client:
        try:
            r = await client.post(config.SEARCH_URL, json=payload, headers=headers)
        except httpx.HTTPError as e:
            return {"errore": f"Impossibile raggiungere SmartLex24: {e}"}

    if r.status_code == 401 or r.status_code == 403:
        # Sessione scaduta lato server nonostante la cache locale: forziamo
        # un nuovo login al prossimo tentativo.
        global _session_token
        _session_token = None
        return {"errore": "Sessione scaduta, riprova: al prossimo tentativo verrà rifatto il login."}

    if r.status_code != 200:
        return {"errore": f"SmartLex24 ha risposto con errore {r.status_code}."}

    try:
        parsed = r.json()
    except ValueError:
        return {"errore": "Risposta inattesa da SmartLex24 (non JSON)."}

    result = parsed.get("Result", {}) or {}
    docs = result.get("Documents", [])
    return {
        "totale_trovati": result.get("DocsFound", len(docs)),
        "did_you_mean": result.get("DidYouMean") or None,
        "risultati": docs,
        "nota": "Struttura dei risultati non ancora interamente mappata: "
        "verifica i campi effettivi restituiti (idDocumento, idProvvedimento, "
        "idFonte...) sui risultati reali di questa chiamata.",
    }


@mcp.tool()
async def cerca_smartlex24(query: str, n: int = 10) -> dict:
    """Cerca su SmartLex24 (Il Sole 24 Ore): giurisprudenza, legge e prassi,
    approfondimenti professionali, secondo l'abbonamento configurato sul
    server.

    ATTENZIONE: connettore sperimentale, appena creato e non ancora
    interamente verificato in tutti i suoi aspetti (struttura dei risultati,
    eventuali filtri disponibili). Il primo utilizzo va trattato come test.

    Args:
        query: testo da cercare.
        n: numero massimo di risultati (default 10, max 20).
    """
    return await _search(query, n)


async def _probe(query: str) -> None:
    import json

    res = await _search(query, 5)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", help="Esegue una ricerca di prova da riga di comando")
    args = parser.parse_args()

    if args.probe:
        asyncio.run(_probe(args.probe))
        sys.exit(0)

    mcp.run(transport=os.getenv("MCP_TRANSPORT", "streamable-http"))
