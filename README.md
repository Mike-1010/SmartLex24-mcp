# Connettore MCP per SmartLex24 (Il Sole 24 Ore)

Strumenti esposti a Claude: `cerca_smartlex24`.

**Richiede un abbonamento SmartLex24 attivo** (username e password). A
differenza di ilcaso.it e italgiure.giustizia.it, qui non c'è ricerca
gratuita: il connettore usa le tue credenziali per accedere.

**Connettore sperimentale**: creato dopo una ricognizione DevTools del
28/09/2026, non ancora testato end-to-end in produzione. La struttura esatta
dei risultati (`risultati`) è quella restituita grezza dal sito e va
verificata al primo uso reale.

## Come funziona (e perché è diverso dagli altri due connettori)

Il login è un form HTML classico:

    POST https://du.ilsole24ore.com/utenti/authfiles/logincentrale.aspx
    campi: txtUsername, txtPassword, realSubmit=BYPASS, RURL, ERRURL, SC=CO, RememberMe=1

Dopo il login, però, la SPA di SmartLex24 genera **lato client**, con
JavaScript, un token di sessione opaco che va incluso in ogni chiamata di
ricerca (endpoint `PullSearch3`). Questo token non è leggibile da una
singola risposta HTTP: nasce dentro il browser. Per questo il connettore usa
un vero browser headless (**Playwright** + Chromium) per fare login e
"catturare" quel token intercettando le chiamate di rete della pagina, una
sola volta ogni 30 minuti circa (`SESSION_TTL`); le ricerche vere e proprie,
una volta ottenuto il token, usano invece semplici richieste HTTP dirette
(più veloci, senza riaprire il browser ogni volta).

Conseguenza pratica: la **prima** ricerca di ogni sessione è lenta (apre un
browser, fa login, aspetta che la pagina carichi le sue chiamate interne),
le successive sono rapide finché il token resta valido.

## 1. Installazione locale

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    playwright install --with-deps chromium

L'ultimo comando scarica il browser Chromium usato da Playwright: senza
questo passaggio il connettore non funziona.

## 2. Credenziali

Imposta le variabili d'ambiente (mai nel codice):

    export SMARTLEX24_USERNAME="tuo_username"
    export SMARTLEX24_PASSWORD="tua_password"

## 3. Prova locale

    python server.py --probe "licenziamento illegittimo"

**Nota**: come per gli altri connettori, in un ambiente con accesso a
internet ristretto (sandbox di sviluppo) questo test potrebbe fallire per
motivi di rete che non hanno nulla a che fare col connettore. Il test che
conta è quello dopo il deploy.

## 4. Collegamento a Claude

**Remoto (Render, Fly.io, Cloud Run...)**: qui c'è un passaggio in più
rispetto agli altri due connettori — il **build command** deve installare
anche il browser, non solo le dipendenze Python. Su Render, imposta:

- **Build Command**: `pip install -r requirements.txt && playwright install --with-deps chromium`
- **Start Command**: `python server.py`
- Variabili d'ambiente: `PORT` (di solito già gestita da Render),
  `MCP_PATH=/un-percorso-segreto/mcp`, `SMARTLEX24_USERNAME`,
  `SMARTLEX24_PASSWORD`

Chromium headless consuma più RAM dei connettori precedenti (che erano solo
richieste HTTP): se il piano Render è troppo piccolo, il login potrebbe
fallire per mancanza di memoria, non per un problema del codice — in quel
caso serve un piano con più RAM.

Poi in Claude: Impostazioni -> Connettori -> Aggiungi connettore personalizzato
-> URL `https://tuo-host/un-percorso-segreto/mcp`.

**Locale (solo Claude Desktop)**: in `claude_desktop_config.json`:

    {"mcpServers": {"smartlex24": {
      "command": "/percorso/.venv/bin/python",
      "args": ["/percorso/server.py"],
      "env": {"MCP_TRANSPORT": "stdio",
               "SMARTLEX24_USERNAME": "...",
               "SMARTLEX24_PASSWORD": "..."}}}}

## Note d'uso e limiti noti

- Se cambi password sul sito, aggiorna la variabile d'ambiente
  `SMARTLEX24_PASSWORD` e riavvia il servizio.
- Se il sito cambia il markup del form di login (id/name dei campi), i
  selettori si possono correggere via variabili d'ambiente senza toccare il
  codice: `SMARTLEX24_LOGIN_USERNAME_SELECTOR`,
  `SMARTLEX24_LOGIN_PASSWORD_SELECTOR`, `SMARTLEX24_LOGIN_SUBMIT_SELECTOR`,
  `SMARTLEX24_LOGIN_ERROR_SELECTOR` (vedi `config.py`).
- La struttura dei singoli risultati (`risultati`) non è stata ancora
  interamente mappata: dopo il primo test reale, va verificato quali campi
  sono davvero utili (es. link al documento integrale) ed eventualmente va
  aggiunto uno strumento per leggere il testo integrale di un singolo
  documento, come `leggi_provvedimento` per il connettore italgiure — non
  ancora implementato qui perché quella parte del sito non è stata ancora
  ispezionata.
- Non è stato ancora verificato cosa succede in caso di 2FA/captcha in fase
  di login: se il sito lo richiede, il login automatico via Playwright
  fallirebbe e andrebbe rivista la strategia.
