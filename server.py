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

Versione 03/11/2026 - correzioni dopo i test dal vivo:
- schede "atto normativo" con campi tutti null: ora si cercano i documenti
  annidati, e una scheda non riconosciuta riporta i nomi dei suoi campi;
- titolo sempre null in ricerca: ricavato dalle schede HTML, più un campo
  "intestazione" (autorità + data) sempre presente;
- documenti "Integrale in PDF" restituiti vuoti: ora il testo viene estratto
  dal PDF, oppure si segnala chiaramente che il formato è PDF;
- diagnostica_login disattivata di default e senza frammenti del token;
- rimosso il campo "visibile" (inaffidabile) e ripulito l'HTML dagli estratti;
- validazione locale dell'id documento.
"""
import argparse
import asyncio
import html
import io
import os
import re
import sys
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

import config

INSTRUCTIONS = """\
Accesso a SmartLex24 (Il Sole 24 Ore) con le credenziali di abbonamento
configurate sul server. Copre giurisprudenza, legge e prassi, approfondimenti
professionali, secondo quanto incluso nell'abbonamento collegato.

Usa `cerca_smartlex24` per le ricerche testuali e `leggi_documento_smartlex24`
per il testo integrale di un documento trovato (passandogli il suo
"idDocumento"). I risultati con "apribile": false non hanno un idDocumento e
non si possono leggere.

Connettore sperimentale: trattare i risultati come indicativi e verificarli
sulla fonte quando serve certezza. Il primo login di una sessione è lento
(apre un vero browser per autenticarsi): le ricerche successive sono più
rapide perché riusano la sessione già ottenuta.
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

_ID_DOCUMENTO_RE = re.compile(r"^\d{1,12}$")
_CHIAVI_ID = ("idDocumento", "IdDocumento", "DocumentId", "documentId", "documentid")
_CHIAVI_TITOLO = ("titolo", "Titolo", "title", "Title", "titoloDocumento", "TitoloDocumento")


class LoginError(Exception):
    """Il login non è andato a buon fine, o non è stato possibile catturare
    il token di sessione dopo un login apparentemente riuscito."""


# ---------------------------------------------------------------------------
# Sessione (login via browser headless)
# ---------------------------------------------------------------------------

async def _esegui_login(page, on_request) -> None:
    """Passi di login comuni a sessione normale e diagnostica."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    await page.goto(
        config.LOGIN_PAGE_URL,
        timeout=config.NAV_TIMEOUT_MS,
        wait_until="domcontentloaded",
    )
    await page.fill(config.LOGIN_USERNAME_SELECTOR, config.SMARTLEX24_USERNAME)
    await page.fill(config.LOGIN_PASSWORD_SELECTOR, config.SMARTLEX24_PASSWORD)

    # Il widget cookie OneTrust (banner o Preference Center con overlay
    # scuro) copre il bottone "Accedi": lo rimuoviamo dal DOM, qualunque
    # variante compaia.
    try:
        await page.evaluate("document.getElementById('onetrust-consent-sdk')?.remove()")
    except Exception:
        pass

    # In ascolto solo da ORA: la pagina di login invia già, prima di
    # autenticarsi, chiamate con un token anonimo/ospite (scoperto il
    # 29/09/2026) che non va catturato.
    page.on("request", on_request)
    await page.click(config.LOGIN_SUBMIT_SELECTOR, force=True)

    try:
        await page.wait_for_load_state("networkidle", timeout=config.NAV_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass  # la SPA può tenere connessioni aperte: non è di per sé un errore

    try:
        error_el = page.locator(config.LOGIN_ERROR_SELECTOR)
        if await error_el.is_visible(timeout=2000):
            msg = (await error_el.inner_text()).strip()
            if msg:
                raise LoginError(f"Login rifiutato da SmartLex24: {msg}")
    except PlaywrightTimeoutError:
        pass

    # Attesa SEMPRE: un'eventuale chiamata di refresh del token, successiva
    # alla prima, deve fare in tempo a sovrascrivere il token catturato.
    await page.wait_for_timeout(config.LOGIN_WAIT_MS)


def _intercetta_token(destinazione: dict):
    """Crea un listener che salva in destinazione["token"] l'ULTIMO token
    visto nelle chiamate REST del sito (il sito lo rigenera dopo il login)."""

    def _on_request(request):
        if request.method != "POST" or "dwa.ilsole24ore.com/dir/api/" not in request.url:
            return
        try:
            data = request.post_data_json
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("token"):
            destinazione["token"] = data["token"]
            destinazione["visti"] = destinazione.get("visti", 0) + 1

    return _on_request


async def _login_and_capture_token() -> tuple[str, list]:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

    if not config.SMARTLEX24_USERNAME or not config.SMARTLEX24_PASSWORD:
        raise LoginError(
            "Credenziali non configurate: imposta le variabili d'ambiente "
            "SMARTLEX24_USERNAME e SMARTLEX24_PASSWORD sul server."
        )

    catturato: dict = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await _esegui_login(page, _intercetta_token(catturato))
        except PlaywrightTimeoutError as e:
            raise LoginError(f"Timeout durante il login: {e}") from e
        finally:
            cookies = await context.cookies()
            await browser.close()

    if not catturato.get("token"):
        raise LoginError(
            "Login apparentemente riuscito ma non è stato possibile catturare "
            "il token di sessione dalle chiamate della pagina (il sito potrebbe "
            "aver cambiato il proprio funzionamento interno: da riverificare "
            "con DevTools)."
        )
    return catturato["token"], cookies


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


def _invalida_sessione() -> None:
    global _session_token
    _session_token = None


def _cookie_header(cookies: Optional[list]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in (cookies or []))


def _api_headers() -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "jsonorb-apikey": config.JSONORB_API_KEY,
        # "false": con "true" il gateway sembrava servire una risposta
        # cache-ata generica, identica per ogni query (osservato il 29/09/2026).
        "jsonorb-addcache": "false",
        "Origin": config.API_ORIGIN,
        "Referer": config.API_REFERER,
    }
    if _session_cookies:
        headers["Cookie"] = _cookie_header(_session_cookies)
    return headers


# ---------------------------------------------------------------------------
# Funzioni di parsing pure (testabili senza rete)
# ---------------------------------------------------------------------------

def _html_a_testo(testo_html: str) -> str:
    """Conversione minima da HTML a testo leggibile (paragrafi preservati)."""
    if not testo_html:
        return ""
    testo = re.sub(r"<br\s*/?>", "\n", testo_html, flags=re.IGNORECASE)
    testo = re.sub(r"</(?:p|h[1-6]|div|li)\s*>", "\n\n", testo, flags=re.IGNORECASE)
    testo = re.sub(r"<[^>]+>", "", testo)
    testo = html.unescape(testo).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", testo).strip()


def _html_a_testo_inline(testo_html: str) -> str:
    """Come sopra ma su una riga (titoli, estratti): spazi multipli collassati."""
    if not testo_html:
        return ""
    testo = re.sub(r"<[^>]+>", " ", testo_html)
    testo = html.unescape(testo)
    return re.sub(r"\s+", " ", testo).strip()


def _ultimo_titolo_da_html(testo_html: str) -> Optional[str]:
    """Il titolo/massima di un documento letto con GetDocument sta in un
    frammento HTML ("Text2") con più blocchi: un'etichetta ("Integrale",
    "Massima redazionale") e poi il titolo vero in un <p>/<h1>. Prendiamo
    l'ULTIMO blocco, o tutto il testo spogliato dai tag se non ce ne sono."""
    if not testo_html:
        return None
    blocchi = re.findall(
        r"<(?:h1|p)[^>]*>(.*?)</(?:h1|p)>", testo_html, flags=re.IGNORECASE | re.DOTALL
    )
    if not blocchi:
        return _html_a_testo_inline(testo_html) or None
    ultimo = _html_a_testo_inline(blocchi[-1])
    # Per le norme l'ultimo blocco è la nota di vigenza ("Testo in vigore dal
    # 21 aprile 1942", osservato il 03/11/2026), non il titolo: in quel caso
    # teniamo l'intero frammento (libro, titolo, articolo, rubrica, vigenza).
    if re.match(r"^(testo\s+)?(in\s+vigore|vigente|abrogat)", ultimo, flags=re.IGNORECASE):
        return _html_a_testo_inline(testo_html) or None
    return ultimo or None


_NCARD_TITOLO_RE = re.compile(
    r"<h3[^>]*class=\"[^\"]*ncard-title[^\"]*\"[^>]*>(.*?)</h3>", re.IGNORECASE | re.DOTALL
)
_NCARD_SOTTOTITOLO_RE = re.compile(
    r"<p[^>]*class=\"[^\"]*ncard-subtitle[^\"]*\"[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL
)


def _leggi_scheda(testo_html: str) -> tuple[Optional[str], bool]:
    """Le "schede" dei risultati (osservate il 03/11/2026) hanno questa forma:
        <h3 class="ncard-title">Articolo 2043</h3>
        <p class="ncard-subtitle">Risarcimento per fatto illecito</p>
    oppure, per le sentenze disponibili solo in PDF:
        <h3 class="ncard-title">Integrale in PDF</h3>
        <p class="ncard-subtitle ...">Responsabilità civile - ... - CTU</p>
    Restituisce (titolo, è_pdf)."""
    if not testo_html or "ncard-" not in testo_html:
        return None, False
    m_tit = _NCARD_TITOLO_RE.search(testo_html)
    m_sub = _NCARD_SOTTOTITOLO_RE.search(testo_html)
    tit = _html_a_testo_inline(m_tit.group(1)) if m_tit else ""
    sub = _html_a_testo_inline(m_sub.group(1)) if m_sub else ""
    is_pdf = "pdf" in tit.lower()
    if is_pdf or tit.lower().startswith(("integrale", "massima")):
        # L'etichetta ("Integrale in PDF") non è un titolo: conta il sottotitolo.
        return (sub or None), is_pdf
    parti = [x for x in (tit, sub) if x]
    return (" - ".join(parti) or None), is_pdf


def _senza_prefisso_numerico(valore: Optional[str]) -> Optional[str]:
    """'152/Giurisprudenza/Corte di Cassazione civile' -> parti senza il
    codice numerico iniziale (interno al sito, inutile per chi legge)."""
    if not valore:
        return None
    parti = [p.strip() for p in str(valore).split("/") if p.strip()]
    if parti and parti[0].isdigit():
        parti = parti[1:]
    return "/".join(parti) or None


def _data_iso(valore: Any) -> Optional[str]:
    """'2026-05-24T00:00:00Z' -> '2026-05-24'; le date "vuote" del sito
    ('0001-01-01...') diventano None."""
    if not valore:
        return None
    s = str(valore)
    if s.startswith("0001"):
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else s


def _intestazione(tipologia: Optional[str], data: Optional[str]) -> Optional[str]:
    """Intestazione leggibile sempre presente (anche quando il sito non dà un
    titolo): 'Corte di Cassazione civile, Sezione 3 - 24/05/2026'."""
    parti = (tipologia or "").split("/")
    # la prima parte è la famiglia (es. "Giurisprudenza"): la togliamo se ce
    # ne sono altre più specifiche
    autorita = ", ".join(p for p in parti[1:] if p) if len(parti) > 1 else (tipologia or "")
    data_it = None
    if data and re.match(r"\d{4}-\d{2}-\d{2}$", data):
        a, m, g = data.split("-")
        data_it = f"{g}/{m}/{a}"
    out = " - ".join(x for x in (autorita, data_it) if x)
    return out or None


def _id_di(d: dict) -> Optional[Any]:
    for k in _CHIAVI_ID:
        v = d.get(k)
        if v not in (None, "", 0, "0"):
            return v
    return None


def _figli_con_id(obj: Any, profondita: int = 0) -> list:
    """Cerca, dentro una scheda senza id, i documenti annidati che un id ce
    l'hanno (es. le singole voci di una scheda "atto normativo")."""
    if profondita > 3:
        return []
    trovati = []
    valori = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else []
    for v in valori:
        candidati = v if isinstance(v, list) else [v]
        for item in candidati:
            if not isinstance(item, dict):
                continue
            if _id_di(item) is not None:
                trovati.append(item)
            else:
                trovati.extend(_figli_con_id(item, profondita + 1))
    return trovati


def _appiattisci(docs: list) -> list:
    """Le schede senza id (osservate il 03/11/2026 per atti normativi interi,
    es. 'Regolamento UE 1215/2012') vengono sostituite dai documenti
    annidati, se ce ne sono; altrimenti restano, marcate come non apribili."""
    out = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        if _id_di(d) is not None:
            out.append(d)
            continue
        figli = _figli_con_id(d)
        out.extend(figli if figli else [d])
    return out


def _normalizza_documento(d: dict) -> dict:
    """Unifica i diversi formati di risultato in un'unica struttura."""
    grezzo = d.get("abstract") or d.get("Abstract") or d.get("text2") or d.get("Text2") or ""
    titolo_scheda, is_pdf = _leggi_scheda(grezzo)

    titolo = next((d.get(k) for k in _CHIAVI_TITOLO if d.get(k)), None)
    if titolo:
        titolo = _html_a_testo_inline(str(titolo))
    else:
        titolo = titolo_scheda or (
            _ultimo_titolo_da_html(d.get("text2") or d.get("Text2") or "")
            if not is_pdf else None
        )

    tipologia = _senza_prefisso_numerico(d.get("tipologia"))
    data = _data_iso(d.get("data"))
    url = d.get("url") or None
    if url and str(url).lower().endswith(".pdf"):
        is_pdf = True

    # L'estratto non deve ripetere la scheda HTML (già diventata titolo).
    estratto = "" if titolo_scheda else _html_a_testo_inline(grezzo)
    troncato = len(estratto) > 800
    id_doc = _id_di(d)

    risultato = {
        "idDocumento": id_doc,
        "apribile": id_doc is not None,
        "titolo": titolo or None,
        "intestazione": _intestazione(tipologia, data),
        "tipologia": tipologia,
        "data": data,
        "famiglia": _senza_prefisso_numerico(d.get("famigliaCode") or d.get("famiglia")),
        "formato": "pdf" if is_pdf else None,
        "url": url,
        "argomento": d.get("argomento") or None,
        "rank": d.get("rank"),
        "estratto": estratto[:800] + "…" if troncato else estratto,
    }
    if id_doc is None:
        # Scheda non ancora mappata (atti normativi interi, 03/11/2026). Per
        # mapparla serve vedere i valori dei campi candidati: ne mostriamo
        # un'anteprima breve (metadati di catalogo, nessun dato personale).
        risultato["anteprima_campi"] = _anteprima_scheda(d)
    return risultato


_CAMPI_ANTEPRIMA = (
    "idProvvedimento", "numeroDocumento", "idDocType", "idFonte",
    "numFonte2", "exArticolo", "text1", "text2", "abstract2", "tipologia",
    "data", "url", "sezione", "sottoFamiglia", "origin", "extension",
)


def _anteprima_scheda(d: dict) -> dict:
    anteprima = {}
    for k in _CAMPI_ANTEPRIMA:
        v = d.get(k)
        if v in (None, "", [], {}):
            continue
        testo = _html_a_testo_inline(str(v)) if isinstance(v, str) else v
        if isinstance(testo, str) and len(testo) > 150:
            testo = testo[:150] + "…"
        anteprima[k] = testo
    correlati = d.get("RelatedDocuments")
    if isinstance(correlati, list):
        anteprima["RelatedDocuments_numero"] = len(correlati)
        if correlati and isinstance(correlati[0], dict):
            primo = correlati[0]
            anteprima["RelatedDocuments_campi"] = sorted(str(k) for k in primo.keys())
            anteprima["RelatedDocuments_primo"] = {
                k: (str(v)[:100] if isinstance(v, str) else v)
                for k, v in primo.items()
                if isinstance(v, (str, int, float, bool)) and v not in ("", None)
            }
    elif correlati not in (None, "", {}):
        anteprima["RelatedDocuments_tipo"] = type(correlati).__name__
    return anteprima


def _trova_url_pdf(obj: Any, profondita: int = 0) -> Optional[str]:
    """Cerca un URL .pdf in qualunque campo testuale della risposta."""
    if profondita > 4:
        return None
    if isinstance(obj, str):
        m = re.search(r"https?://[^\s\"'<>]+?\.pdf\b", obj, flags=re.IGNORECASE)
        return m.group(0) if m else None
    valori = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else []
    for v in valori:
        trovato = _trova_url_pdf(v, profondita + 1)
        if trovato:
            return trovato
    return None


def _url_pdf_dedotto(id_documento: str) -> str:
    return f"{config.PDF_BASE_URL}/{id_documento[-1]}/{id_documento}.PDF"


def _host_consentito(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host == h or host.endswith("." + h) for h in config.PDF_HOST_CONSENTITI
    )


def _estrai_testo_pdf(contenuto: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(contenuto))
    pagine = [(pagina.extract_text() or "").strip() for pagina in reader.pages]
    pagine = _togli_intestazioni_ripetute(pagine)
    testo = "\n\n".join(p for p in pagine if p)
    testo = re.sub(r"[ \t]+\n", "\n", testo)
    return re.sub(r"\n{3,}", "\n\n", testo).strip()


def _togli_intestazioni_ripetute(pagine: list) -> list:
    """Toglie le righe che si ripetono uguali su molte pagine (firma digitale,
    numeri di registro e data di pubblicazione in calce a ogni pagina delle
    sentenze della Cassazione, osservati il 03/11/2026) e i numeri di pagina
    isolati. La prima occorrenza di ogni riga ripetuta viene conservata, così
    i dati (es. "Data pubblicazione 02/03/2026") restano nel testo una volta."""
    if len(pagine) < 3:
        return pagine
    conteggio: dict = {}
    for p in pagine:
        for riga in {r.strip() for r in p.splitlines() if r.strip()}:
            conteggio[riga] = conteggio.get(riga, 0) + 1
    soglia = max(3, len(pagine) // 2)
    ripetute = {r for r, n in conteggio.items() if n >= soglia}
    gia_viste: set = set()
    risultato = []
    for p in pagine:
        righe = []
        for riga in p.splitlines():
            r = riga.strip()
            if re.fullmatch(r"\d{1,3}", r):
                continue
            if r in ripetute:
                if r in gia_viste:
                    continue
                gia_viste.add(r)
            righe.append(riga)
        risultato.append("\n".join(righe).strip())
    return risultato


# ---------------------------------------------------------------------------
# Chiamate al sito
# ---------------------------------------------------------------------------

async def _scarica_testo_pdf(url: str) -> dict:
    """Scarica un PDF (solo da host del Sole 24 Ore) e ne estrae il testo."""
    if not _host_consentito(url):
        return {"errore_pdf": "URL del PDF su un host non consentito."}
    headers = {"Referer": config.API_REFERER}
    if _session_cookies:
        headers["Cookie"] = _cookie_header(_session_cookies)
    try:
        async with httpx.AsyncClient(timeout=config.TIMEOUT, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers) as r:
                if r.status_code != 200:
                    return {"errore_pdf": f"Il PDF ha risposto con errore {r.status_code}."}
                if not _host_consentito(str(r.url)):
                    return {"errore_pdf": "Il PDF reindirizza a un host non consentito."}
                dati = bytearray()
                async for blocco in r.aiter_bytes():
                    dati.extend(blocco)
                    if len(dati) > config.PDF_MAX_BYTES:
                        return {"errore_pdf": "PDF troppo grande per essere letto qui."}
    except httpx.HTTPError as e:
        return {"errore_pdf": f"Impossibile scaricare il PDF: {e}"}

    if not bytes(dati[:5]).startswith(b"%PDF"):
        return {"errore_pdf": "Il file scaricato non è un PDF (forse una pagina di login)."}
    try:
        testo = await asyncio.to_thread(_estrai_testo_pdf, bytes(dati))
    except Exception as e:  # PDF danneggiato o non supportato
        return {"errore_pdf": f"Impossibile estrarre il testo dal PDF: {e}"}
    if not testo:
        return {"errore_pdf": "Il PDF non contiene testo estraibile (probabilmente è una scansione)."}
    troncato = len(testo) > config.MAX_CARATTERI_PDF
    return {"testo": testo[: config.MAX_CARATTERI_PDF], "troncato": troncato}


async def _chiama_get_document(id_documento: str, token: str) -> dict:
    """Chiamata grezza a GetDocument: restituisce {"result": ...} oppure
    {"errore": ...}. Struttura verificata via DevTools il 29/09/2026."""
    payload = {
        "documentId": id_documento,
        "parameters": {"references": False, "checkUserPackages": True, "staticToken": ""},
        "token": token,
    }
    async with httpx.AsyncClient(timeout=config.TIMEOUT) as client:
        try:
            r = await client.post(config.DOCUMENT_URL, json=payload, headers=_api_headers())
        except httpx.HTTPError as e:
            return {"errore": f"Impossibile raggiungere SmartLex24: {e}"}

    if r.status_code in (401, 403):
        _invalida_sessione()
        return {"errore": "Sessione scaduta, riprova: al prossimo tentativo verrà rifatto il login."}
    if r.status_code == 400:
        return {"errore": "SmartLex24 ha rifiutato la richiesta (errore 400): probabilmente "
                          "l'idDocumento non esiste."}
    if r.status_code != 200:
        return {"errore": f"SmartLex24 ha risposto con errore {r.status_code}."}
    try:
        parsed = r.json()
    except ValueError:
        return {"errore": "Risposta inattesa da SmartLex24 (non JSON)."}
    result = parsed.get("Result") or {}
    if not result:
        return {"errore": "Documento non trovato o risposta vuota."}
    return {"result": result}


def _risposta_da_result(result: dict) -> dict:
    testo = _html_a_testo(result.get("TestoDoc") or "")
    return {
        "idDocumento": result.get("DocumentId"),
        "titolo": _ultimo_titolo_da_html(result.get("Text2") or ""),
        "famiglia": result.get("Famiglia"),
        "sottofamiglia": result.get("SottoFamiglia"),
        # "Blocked" pare il vero indicatore di accesso negato (da confermare
        # su un documento realmente fuori abbonamento).
        "accesso_negato": bool(result.get("Blocked")),
        "formato": "html" if testo else None,
        "testo_integrale": testo,
        "id_precedente": result.get("DocumentIdPrev") or None,
        "id_successivo": result.get("DocumentIdNext") or None,
    }


def _stesso_titolo(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    norm = lambda t: re.sub(r"\s+", " ", t).strip().lower()
    return norm(a) == norm(b)


async def _cerca_gemello_html(risposta: dict, token: str) -> Optional[dict]:
    """Molte sentenze sono pubblicate due volte, una in PDF e una in HTML,
    con lo stesso titolo e id adiacenti (es. 44883625 PDF / 44890439 HTML,
    osservato il 03/11/2026). La versione HTML è preferibile: testo pulito e
    nomi delle parti oscurati. La cerchiamo tra il documento precedente e il
    successivo, accettandola solo se il titolo coincide."""
    for chiave in ("id_successivo", "id_precedente"):
        id_vicino = risposta.get(chiave)
        if not id_vicino or not _ID_DOCUMENTO_RE.match(str(id_vicino)):
            continue
        esito = await _chiama_get_document(str(id_vicino), token)
        if "result" not in esito:
            continue
        vicino = _risposta_da_result(esito["result"])
        if (vicino["testo_integrale"] and not vicino["accesso_negato"]
                and _stesso_titolo(vicino["titolo"], risposta["titolo"])):
            return vicino
    return None


async def _get_document(id_documento: str) -> dict:
    id_documento = str(id_documento).strip()
    if not _ID_DOCUMENTO_RE.match(id_documento):
        return {"errore": "idDocumento non valido: deve essere un numero (es. 45464265), "
                          "preso da un risultato di cerca_smartlex24."}

    try:
        token = await ensure_session()
    except LoginError as e:
        return {"errore": str(e)}

    esito = await _chiama_get_document(id_documento, token)
    if "result" not in esito:
        return esito
    result = esito["result"]
    risposta = _risposta_da_result(result)

    if risposta["testo_integrale"] or risposta["accesso_negato"]:
        return risposta

    # Testo vuoto e documento non bloccato: quasi sempre un "Integrale in PDF".
    url_pdf = _trova_url_pdf(result)
    dedotto = url_pdf is None
    if dedotto:
        url_pdf = _url_pdf_dedotto(id_documento)

    gemello = await _cerca_gemello_html(risposta, token)
    if gemello:
        gemello["id_richiesto"] = risposta["idDocumento"]
        gemello["url_pdf"] = url_pdf
        gemello["nota"] = (
            f"Il documento richiesto ({risposta['idDocumento']}) è disponibile solo in PDF: "
            f"questo è il testo della versione HTML della stessa decisione "
            f"(idDocumento {gemello['idDocumento']}, stesso titolo), con i nomi delle "
            "parti oscurati. L'originale firmato è in url_pdf."
        )
        return gemello

    pdf = await _scarica_testo_pdf(url_pdf)
    risposta["formato"] = "pdf"
    risposta["url_pdf"] = url_pdf
    risposta["url_pdf_dedotto"] = dedotto
    if "testo" in pdf:
        risposta["testo_integrale"] = pdf["testo"]
        risposta["testo_troncato"] = pdf["troncato"]
        risposta["nota"] = (
            "Testo estratto automaticamente dal PDF (impaginazione e a capo possono "
            "essere imperfetti). ATTENZIONE: il PDF è l'originale e può contenere i nomi "
            "delle parti non oscurati: non riportarli in atti o testi destinati a terzi."
        )
    else:
        risposta["nota"] = (
            "Il testo integrale è disponibile solo in PDF e non è stato possibile "
            f"estrarlo ({pdf['errore_pdf']}). Il testo vuoto NON significa documento "
            "senza contenuto: apri url_pdf."
        )
    return risposta


async def _search(query: str, n: int) -> dict:
    query = (query or "").strip()
    if not query:
        return {"errore": "La query è vuota."}

    try:
        token = await ensure_session()
    except LoginError as e:
        return {"errore": str(e)}

    n_effettivo = max(1, min(int(n), config.MAX_ROWS))
    # Payload verificato via DevTools il 29/09/2026: il testo va in
    # "queryWord" (non "queryText", ignorato in silenzio dal sito).
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
            "rows": n_effettivo,
            "start": 0,
            "use_pcpa": False,
        },
        "token": token,
    }
    async with httpx.AsyncClient(timeout=config.TIMEOUT) as client:
        try:
            r = await client.post(config.SEARCH_URL, json=payload, headers=_api_headers())
        except httpx.HTTPError as e:
            return {"errore": f"Impossibile raggiungere SmartLex24: {e}"}

    if r.status_code in (401, 403):
        _invalida_sessione()
        return {"errore": "Sessione scaduta, riprova: al prossimo tentativo verrà rifatto il login."}
    if r.status_code != 200:
        return {"errore": f"SmartLex24 ha risposto con errore {r.status_code}."}

    try:
        parsed = r.json()
    except ValueError:
        return {"errore": "Risposta inattesa da SmartLex24 (non JSON)."}

    result = parsed.get("Result", {}) or {}
    docs = _appiattisci(result.get("Documents", []) or [])
    # "rows" non è sempre rispettato dal sito: tagliamo qui.
    risultati = [_normalizza_documento(d) for d in docs[:n_effettivo]]

    return {
        "totale_trovati": result.get("DocsFound", len(risultati)),
        "did_you_mean": result.get("DidYouMean") or None,
        "risultati": risultati,
        "nota": "Per il testo integrale usa leggi_documento_smartlex24 con l'idDocumento. "
                "'intestazione' è ricavata da tipologia e data del sito; 'formato': 'pdf' "
                "indica un documento disponibile solo in PDF. I risultati con "
                "'apribile': false non si possono leggere.",
    }


# ---------------------------------------------------------------------------
# Strumenti MCP
# ---------------------------------------------------------------------------

@mcp.tool()
async def cerca_smartlex24(query: str, n: int = 10) -> dict:
    """Cerca su SmartLex24 (Il Sole 24 Ore): giurisprudenza, legge e prassi,
    approfondimenti professionali, secondo l'abbonamento configurato sul
    server.

    Ogni risultato ha gli stessi campi: idDocumento, apribile, titolo,
    intestazione (autorità e data, sempre presente quando il sito le
    fornisce), tipologia, data (AAAA-MM-GG), famiglia, formato ("pdf" per i
    documenti disponibili solo in PDF), url, argomento, rank, estratto.
    Il titolo può mancare per le sentenze: in quel caso usa intestazione ed
    estratto. "estratto" è solo un'anteprima: per il testo integrale usa
    leggi_documento_smartlex24 con l'"idDocumento".

    La ricerca non ha filtri: parole come "Cassazione" nella query non
    restringono i risultati alla sola Cassazione.

    Connettore sperimentale: trattare i risultati come indicativi e
    verificarli sulla fonte quando serve certezza.

    Args:
        query: testo da cercare.
        n: numero massimo di risultati (default 10, max 20).
    """
    return await _search(query, n)


@mcp.tool()
async def leggi_documento_smartlex24(id_documento: str) -> dict:
    """Legge il testo integrale di un documento SmartLex24, dato il suo
    "idDocumento" (restituito da un risultato di cerca_smartlex24).

    - "accesso_negato": true -> documento non incluso nell'abbonamento,
      "testo_integrale" vuoto.
    - Documento disponibile solo in PDF: se esiste la versione HTML della
      stessa decisione (documento adiacente con lo stesso titolo) viene
      restituita quella, con "id_richiesto" e "url_pdf" dell'originale;
      altrimenti "formato": "pdf" e il testo è estratto dal PDF, che può
      contenere i nomi delle parti NON oscurati. Se l'estrazione non è
      riuscita, "nota" lo spiega e "testo_integrale" è vuoto: NON significa
      documento senza contenuto.
    - Schede senza idDocumento ("apribile": false) non si possono leggere.

    Args:
        id_documento: l'idDocumento numerico di un risultato di cerca_smartlex24.
    """
    return await _get_document(id_documento)


async def diagnostica_login() -> dict:
    """Diagnostica (solo debug del connettore, non per l'utente finale):
    esegue il login e dice se è andato a buon fine - URL e titolo della
    pagina finale, se il form di login è ancora presente, se un token è
    stato catturato e quanti ne sono passati. Non mostra il token né i
    cookie. Disponibile solo con SMARTLEX24_ABILITA_DIAGNOSTICA=1."""
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

    if not config.SMARTLEX24_USERNAME or not config.SMARTLEX24_PASSWORD:
        return {"errore": "Credenziali non configurate."}

    catturato: dict = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            try:
                await _esegui_login(page, _intercetta_token(catturato))
            except LoginError as e:
                return {"errore": str(e)}
            except PlaywrightTimeoutError as e:
                return {"errore": f"Timeout durante il login: {e}"}
            final_url = page.url
            final_title = await page.title()
            form_presente = False
            try:
                form_presente = await page.locator(config.LOGIN_USERNAME_SELECTOR).is_visible(timeout=1000)
            except Exception:
                pass
            cookies = await context.cookies()
        finally:
            await browser.close()

    return {
        "url_finale": final_url,
        "titolo_pagina_finale": final_title,
        "form_login_ancora_presente": form_presente,
        "token_catturato": bool(catturato.get("token")),
        "token_lunghezza": len(catturato.get("token") or ""),
        "numero_token_visti": catturato.get("visti", 0),
        "numero_cookie": len(cookies),
    }


if config.ABILITA_DIAGNOSTICA:
    mcp.tool()(diagnostica_login)


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

    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    if transport != "stdio" and os.getenv("MCP_PATH", "/mcp") == "/mcp":
        print(
            "ATTENZIONE: MCP_PATH non è impostato a un percorso segreto: chiunque "
            "conosca l'indirizzo del server può usare il tuo abbonamento.",
            file=sys.stderr,
        )
    mcp.run(transport=transport)
