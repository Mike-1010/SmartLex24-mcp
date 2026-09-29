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
import html
import os
import re
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

Usa `cerca_smartlex24` per le ricerche testuali e `leggi_documento_smartlex24`
per il testo integrale di un documento trovato (passandogli il suo
"idDocumento").

ATTENZIONE - connettore sperimentale, in fase di prima verifica: il campo
"visibile" nei risultati di ricerca non è affidabile per sapere se un
documento è incluso nell'abbonamento (usa invece "accesso_negato" restituito
da leggi_documento_smartlex24). Il primo login di una sessione è lento (apre
un vero browser per autenticarsi): le ricerche successive sono più rapide
perché riusano la sessione già ottenuta.
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
        if request.method != "POST":
            return
        if "dwa.ilsole24ore.com/dir/api/" not in request.url:
            return
        try:
            data = request.post_data_json
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("token"):
            # NOTA (29/09/2026): non ci fermiamo al primo token visto - il
            # sito sembra rigenerare/aggiornare il token con chiamate
            # successive al login (es. RefreshToken); prendiamo sempre
            # l'ULTIMO visto, presumendolo il più aggiornato/valido.
            captured_token = data["token"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        # NOTA (scoperto il 29/09/2026): la pagina di login fa già delle
        # chiamate POST con un "token" nel payload PRIMA di autenticarsi (per
        # mostrare un'anteprima anonima/ospite della pagina). Se ci mettiamo
        # in ascolto da subito, rischiamo di catturare quel token anonimo
        # invece di quello vero generato dopo il login riuscito - risultato:
        # le ricerche "funzionano" (nessun errore) ma restituiscono un
        # catalogo generico non personalizzato, invece dei risultati
        # dell'abbonamento. Ci mettiamo in ascolto solo DOPO aver cliccato
        # "Accedi", scartando ogni token visto prima di quel momento.

        try:
            await page.goto(
                config.LOGIN_PAGE_URL,
                timeout=config.NAV_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
            await page.fill(config.LOGIN_USERNAME_SELECTOR, config.SMARTLEX24_USERNAME)
            await page.fill(config.LOGIN_PASSWORD_SELECTOR, config.SMARTLEX24_PASSWORD)

            # Il widget cookie OneTrust (banner o, a volte, il Preference
            # Center con un proprio overlay scuro "onetrust-pc-dark-filter")
            # copre il bottone "Accedi" e ne blocca il click. Invece di
            # inseguire quale dei due componenti OneTrust sta mostrando (i
            # loro bottoni hanno id diversi), lo rimuoviamo proprio dal DOM:
            # più robusto, non dipende da quale variante compare.
            try:
                await page.evaluate(
                    "document.getElementById('onetrust-consent-sdk')?.remove()"
                )
            except Exception:
                pass

            # Ci mettiamo in ascolto delle richieste solo ORA, subito prima di
            # cliccare "Accedi": qualunque token visto da questo momento in
            # poi è (o dovrebbe essere) generato dopo un login riuscito, non
            # prima.
            page.on("request", _on_request)

            # Rete di sicurezza: se per qualunque motivo un altro elemento
            # dovesse comunque intercettare il click, forziamo il click
            # bypassando il controllo "receives pointer events" di Playwright.
            await page.click(config.LOGIN_SUBMIT_SELECTOR, force=True)

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

            # Attesa aggiuntiva SEMPRE (non solo se manca ancora un token):
            # vogliamo dare tempo a un'eventuale chiamata di refresh/rinnovo
            # del token, successiva al primo, di completarsi e sovrascrivere
            # "captured_token" con il valore più aggiornato.
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


def _ultimo_titolo_da_html(testo_html: str) -> Optional[str]:
    """Il titolo/massima non è in un campo a sé (osservato il 29/09/2026): è
    dentro un frammento HTML ("text2"/"Text2") con più blocchi - un'etichetta
    tipo "Massima redazionale"/"Integrale" seguita dal titolo vero e proprio
    in un <p> o <h1>. Prendiamo l'ULTIMO blocco <p>/<h1> trovato (di norma il
    titolo, non l'etichetta), o tutto il testo spogliato dai tag se non ne
    troviamo nessuno."""
    if not testo_html:
        return None
    blocchi = re.findall(
        r"<(?:h1|p)[^>]*>(.*?)</(?:h1|p)>", testo_html, flags=re.IGNORECASE | re.DOTALL
    )
    frammento = blocchi[-1] if blocchi else testo_html
    return _html_a_testo_inline(frammento) or None


def _normalizza_documento(d: dict) -> dict:
    """Unifica in un'unica struttura i due formati osservati nelle risposte
    reali del 29/09/2026 (uno con tipologia/data/rank valorizzati e abstract
    troncato con "...", l'altro con quei campi a null/0 ma un testo più
    esteso in "text2"/abstract, spesso l'inizio della motivazione) - così chi
    consuma i risultati non deve gestire due schemi diversi.

    NOTA: il campo "visibile" non è ancora interpretabile con certezza (non
    segue data/famiglia/tipo, ma il formato del risultato) - non affidarti al
    suo valore per decidere se il testo integrale è incluso nell'abbonamento.
    Usa invece "accesso_negato" restituito da leggi_documento_smartlex24.
    "idDocumento" a 0 o assente significa che il documento non può comunque
    essere aperto con leggi_documento_smartlex24.
    """
    testo = d.get("abstract") or d.get("text2") or d.get("Abstract") or ""
    troncato = len(testo) > 800
    return {
        "idDocumento": d.get("idDocumento") or None,
        "titolo": (
            d.get("titolo")
            or d.get("title")
            or d.get("Title")
            or _ultimo_titolo_da_html(d.get("text2") or "")
        ),
        "tipologia": d.get("tipologia") or None,
        "data": d.get("data") if d.get("data") not in (None, "0001-01-01") else None,
        "famiglia": d.get("famigliaCode") or d.get("famiglia") or None,
        "url": d.get("url") or None,
        "argomento": d.get("argomento") or None,
        "rank": d.get("rank"),
        "visibile": d.get("visibile"),
        "estratto": testo[:800] + "…" if troncato else testo,
        "estratto_troncato_qui": troncato,
    }


def _html_a_testo(testo_html: str) -> str:
    """Conversione minima da HTML a testo leggibile per il campo "TestoDoc"
    (niente dipendenze esterne: solo tag più comuni + unescape entità)."""
    if not testo_html:
        return ""
    testo = re.sub(r"<br\s*/?>", "\n", testo_html, flags=re.IGNORECASE)
    testo = re.sub(r"</p\s*>", "\n\n", testo, flags=re.IGNORECASE)
    testo = re.sub(r"<[^>]+>", "", testo)
    testo = html.unescape(testo)
    return re.sub(r"\n{3,}", "\n\n", testo).strip()


def _html_a_testo_inline(testo_html: str) -> str:
    """Come sopra ma per campi di una riga (es. titoli): spazi al posto di
    newline, spazi multipli collassati."""
    if not testo_html:
        return ""
    testo = re.sub(r"<[^>]+>", " ", testo_html)
    testo = html.unescape(testo)
    return re.sub(r"\s{2,}", " ", testo).strip()


async def _get_document(id_documento: str) -> dict:
    try:
        token = await ensure_session()
    except LoginError as e:
        return {"errore": str(e)}

    # Struttura verificata via DevTools il 29/09/2026 cliccando "Integrale"
    # su un risultato reale: endpoint e payload diversi da PullSearch3 (qui
    # il campo è "documentId", non "queryWord").
    payload = {
        "documentId": str(id_documento),
        "parameters": {
            "references": False,
            "checkUserPackages": True,
            "staticToken": "",
        },
        "token": token,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "jsonorb-apikey": config.JSONORB_API_KEY,
        "jsonorb-addcache": "false",
        "Origin": config.API_ORIGIN,
        "Referer": config.API_REFERER,
    }
    if _session_cookies:
        headers["Cookie"] = _cookie_header(_session_cookies)

    async with httpx.AsyncClient(timeout=config.TIMEOUT) as client:
        try:
            r = await client.post(config.DOCUMENT_URL, json=payload, headers=headers)
        except httpx.HTTPError as e:
            return {"errore": f"Impossibile raggiungere SmartLex24: {e}"}

    if r.status_code in (401, 403):
        global _session_token
        _session_token = None
        return {"errore": "Sessione scaduta, riprova: al prossimo tentativo verrà rifatto il login."}

    if r.status_code != 200:
        return {"errore": f"SmartLex24 ha risposto con errore {r.status_code}."}

    try:
        parsed = r.json()
    except ValueError:
        return {"errore": "Risposta inattesa da SmartLex24 (non JSON)."}

    result = parsed.get("Result") or {}
    if not result:
        return {"errore": "Documento non trovato o risposta vuota."}

    return {
        "idDocumento": result.get("DocumentId"),
        "titolo": _ultimo_titolo_da_html(result.get("Text2") or ""),
        "famiglia": result.get("Famiglia"),
        "sottofamiglia": result.get("SottoFamiglia"),
        # NOTA (29/09/2026): "Blocked" qui sembra essere il vero indicatore
        # di accesso negato dall'abbonamento (a differenza di "visibile" nei
        # risultati di ricerca, il cui significato resta incerto) - da
        # confermare su un documento realmente fuori abbonamento.
        "accesso_negato": bool(result.get("Blocked")),
        "testo_integrale": _html_a_testo(result.get("TestoDoc") or ""),
        "id_precedente": result.get("DocumentIdPrev") or None,
        "id_successivo": result.get("DocumentIdNext") or None,
    }


async def _search(query: str, n: int) -> dict:
    try:
        token = await ensure_session()
    except LoginError as e:
        return {"errore": str(e)}

    # Struttura del payload verificata via DevTools il 29/09/2026 su una
    # ricerca reale: il campo del testo cercato è "queryWord", non
    # "queryText" (che il sito ignora silenziosamente, restituendo un elenco
    # generico invece di un errore). Gli altri campi sono presenti in ogni
    # richiesta reale del sito: li replichiamo com'erano, a parte "rows" e
    # "start" che rendiamo parametrici per la paginazione.
    payload = {
        "parameters": {
            "addQueryExtBuca": False,
            "didYouMean": True,
            "enableQD": True,
            "excludeNotSearchablePackets": True,
            "extraFacet": "",
            "facet": "tipologia",
            "facetMinCount": 1,
            "facetSortIndex": True,
            "groups": True,
            "invokePush": True,
            "loadFacetsTag": False,
            "modulo24Embedded": False,
            "order": "R",
            "orderLucene": "score desc",
            "pacchettiRicercaEstremi": "",
            "proustFilter": True,
            "queryExt": "",
            "queryLucene": "",
            "queryWord": query,
            "rows": max(1, min(n, config.MAX_ROWS)),
            "start": 0,
            "use_pcpa": False,
        },
        "token": token,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "jsonorb-apikey": config.JSONORB_API_KEY,
        # NOTA (29/09/2026): disattivato "true" -> "false". Sospetto che il
        # gateway API stesse servendo una risposta cache-ata generica sempre
        # identica (stessi 5 "Codice penale", stesso rank, a prescindere da
        # query/token), probabilmente perché la chiave di cache usata dal
        # server non include qualcosa che nel nostro caso manca/è costante
        # (es. un cookie di sessione specifico). Da riverificare.
        "jsonorb-addcache": "false",
        "Origin": config.API_ORIGIN,
        "Referer": config.API_REFERER,
    }
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
    docs = result.get("Documents", []) or []

    # Il campo "rows" del payload non è sempre rispettato dal sito (osservato
    # il 29/09/2026: righe annidate possono far superare il numero
    # richiesto), quindi tagliamo qui in modo esplicito a "n effettivo", per
    # rispettare quanto chiesto e limitare la dimensione della risposta.
    n_effettivo = max(1, min(n, config.MAX_ROWS))
    docs = docs[:n_effettivo]

    return {
        "totale_trovati": result.get("DocsFound", len(docs)),
        "did_you_mean": result.get("DidYouMean") or None,
        "risultati": [_normalizza_documento(d) for d in docs],
        "nota": "Struttura dei risultati normalizzata da questo connettore. "
        "Per leggere il testo integrale di un documento usa "
        "leggi_documento_smartlex24 con il suo 'idDocumento' - il campo "
        "'visibile' qui non è affidabile per sapere se è incluso "
        "nell'abbonamento, usa 'accesso_negato' da quello strumento.",
    }


@mcp.tool()
async def cerca_smartlex24(query: str, n: int = 10) -> dict:
    """Cerca su SmartLex24 (Il Sole 24 Ore): giurisprudenza, legge e prassi,
    approfondimenti professionali, secondo l'abbonamento configurato sul
    server.

    Ogni risultato è normalizzato con gli stessi campi (idDocumento, titolo,
    tipologia, data, famiglia, url, argomento, rank, visibile, estratto):
    alcuni possono essere null a seconda del tipo di documento. "n" viene
    sempre rispettato lato connettore (anche quando il sito restituisce più
    righe del richiesto). "estratto" è solo un'anteprima: per il testo
    integrale usa leggi_documento_smartlex24 con l'"idDocumento" del
    risultato che interessa.

    ATTENZIONE: connettore sperimentale, non ancora interamente verificato
    (in particolare il significato del campo "visibile", da non usare per
    sapere se il testo integrale è incluso nell'abbonamento - per quello usa
    "accesso_negato" restituito da leggi_documento_smartlex24). Trattare i
    risultati come indicativi e verificarli sulla fonte quando serve
    certezza.

    Args:
        query: testo da cercare.
        n: numero massimo di risultati (default 10, max 20).
    """
    return await _search(query, n)


@mcp.tool()
async def leggi_documento_smartlex24(id_documento: str) -> dict:
    """Legge il testo integrale di un documento SmartLex24, dato il suo
    "idDocumento" (restituito da un risultato di cerca_smartlex24).

    Se "accesso_negato" è true, il documento non è incluso nell'abbonamento
    collegato (o non è stato possibile leggerlo) e "testo_integrale" sarà
    vuoto: non trattare in quel caso una stringa vuota come "documento senza
    contenuto".

    Args:
        id_documento: l'idDocumento di un risultato di cerca_smartlex24.
    """
    return await _get_document(id_documento)


@mcp.tool()
async def diagnostica_login() -> dict:
    """Diagnostica: esegue il login e restituisce informazioni tecniche per
    capire se è andato davvero a buon fine (URL finale della pagina, se
    l'email dell'utente compare da qualche parte nella pagina, lunghezza e
    inizio/fine del token catturato) - non fa nessuna ricerca. Da usare solo
    per debug del connettore, non è pensato per l'utente finale."""
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

    if not config.SMARTLEX24_USERNAME or not config.SMARTLEX24_PASSWORD:
        return {"errore": "Credenziali non configurate."}

    captured_token: Optional[str] = None
    all_tokens_seen = []

    def _on_request(request):
        nonlocal captured_token
        if request.method != "POST" or "dwa.ilsole24ore.com/dir/api/" not in request.url:
            return
        try:
            data = request.post_data_json
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("token"):
            captured_token = data["token"]
            all_tokens_seen.append({"url": request.url, "token_len": len(data["token"])})

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(config.LOGIN_PAGE_URL, timeout=config.NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.fill(config.LOGIN_USERNAME_SELECTOR, config.SMARTLEX24_USERNAME)
            await page.fill(config.LOGIN_PASSWORD_SELECTOR, config.SMARTLEX24_PASSWORD)
            try:
                await page.evaluate("document.getElementById('onetrust-consent-sdk')?.remove()")
            except Exception:
                pass
            page.on("request", _on_request)
            await page.click(config.LOGIN_SUBMIT_SELECTOR, force=True)
            try:
                await page.wait_for_load_state("networkidle", timeout=config.NAV_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(config.LOGIN_WAIT_MS)

            final_url = page.url
            final_title = await page.title()
            body_text = await page.evaluate("document.body ? document.body.innerText : ''")
            username_visible = config.SMARTLEX24_USERNAME.lower() in (body_text or "").lower()
            # cerchiamo anche solo la parte prima della @ (spesso è quella mostrata)
            username_prefix = config.SMARTLEX24_USERNAME.split("@")[0].lower()
            username_prefix_visible = username_prefix in (body_text or "").lower()
            login_form_still_present = False
            try:
                login_form_still_present = await page.locator(config.LOGIN_USERNAME_SELECTOR).is_visible(timeout=1000)
            except Exception:
                pass

            cookies = await context.cookies()
        finally:
            await browser.close()

    return {
        "url_finale": final_url,
        "titolo_pagina_finale": final_title,
        "email_utente_visibile_in_pagina": username_visible,
        "prefisso_email_visibile_in_pagina": username_prefix_visible,
        "form_login_ancora_presente": login_form_still_present,
        "token_catturato": bool(captured_token),
        "token_lunghezza": len(captured_token) if captured_token else 0,
        "token_inizio": captured_token[:20] if captured_token else None,
        "token_fine": captured_token[-20:] if captured_token else None,
        "numero_token_visti": len(all_tokens_seen),
        "numero_cookie": len(cookies),
        "nomi_cookie": [c["name"] for c in cookies],
    }


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
