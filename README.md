# Connettore MCP per SmartLex24 (Il Sole 24 Ore)

Strumenti esposti a Claude:

- `cerca_smartlex24(query, n)` — ricerca testuale (max 20 risultati). Ogni
  risultato ha: `idDocumento`, `apribile`, `titolo`, `intestazione` (autorità
  e data, sempre presente quando il sito le fornisce), `tipologia`, `data`
  (AAAA-MM-GG), `famiglia`, `formato` (`"pdf"` per i documenti disponibili
  solo in PDF), `url`, `argomento`, `rank`, `estratto` (testo senza HTML).
- `leggi_documento_smartlex24(id_documento)` — testo integrale di un
  documento. Per i documenti "Integrale in PDF" il testo viene estratto dal
  PDF (`formato: "pdf"`, `url_pdf`); se l'estrazione non riesce, il campo
  `nota` lo spiega.
- `diagnostica_login()` — **solo debug, disattivato di default** (vedi sotto).

**Richiede un abbonamento SmartLex24 attivo** (username e password). Il
connettore usa le tue credenziali per accedere.

**Connettore sperimentale**: usa endpoint interni del sito, non un'API
ufficiale. Un aggiornamento del sito può romperlo senza preavviso.

## Novità del 03/11/2026

Correzioni dopo i test dal vivo:

1. Le schede "atto normativo" (es. ricerca "Regolamento UE 1215/2012")
   arrivavano con tutti i campi `null`. Ora vengono sostituite dai documenti
   annidati, se ci sono; altrimenti restano con `apribile: false` e
   un riepilogo dei campi per poterle mappare.
2. `titolo` era sempre `null` in ricerca: ora è ricavato dalle schede del
   sito quando c'è, e in ogni caso c'è il nuovo campo `intestazione`.
3. I documenti "Integrale in PDF" tornavano con testo vuoto: ora il testo è
   estratto dal PDF (libreria `pypdf`).
4. `diagnostica_login` è disattivata di default e non mostra più parti del
   token né i nomi dei cookie.
5. Rimosso il campo `visibile` (inaffidabile); tolto l'HTML dagli estratti.
6. L'`id_documento` è validato prima di contattare il sito.

### Secondo aggiornamento (03/11/2026, dopo i test dal vivo)

- Documenti solo in PDF: prima si cerca la versione HTML della stessa
  decisione (documento adiacente con lo stesso titolo), più pulita e con i
  nomi delle parti oscurati; il PDF resta indicato in `url_pdf`.
- Testo estratto dai PDF: tolte le righe ripetute in calce a ogni pagina
  (firma digitale, numeri di registro) e i numeri di pagina.
- Titolo delle norme: non più solo "Testo in vigore dal ...", ma libro,
  titolo, articolo e rubrica.
- Schede degli atti normativi interi: al posto di `campi_grezzi` c'è
  `anteprima_campi`, con i valori brevi dei campi candidati, per poterle
  rendere apribili nel prossimo aggiornamento.

## Come funziona

Il login è un form HTML classico:

    POST https://du.ilsole24ore.com/utenti/authfiles/logincentrale.aspx

Dopo il login, la SPA di SmartLex24 genera **lato client** un token di
sessione opaco, incluso in ogni chiamata di ricerca (endpoint `PullSearch3`).
Il connettore usa un browser headless (**Playwright** + Chromium) per fare
login e catturare quel token, una volta ogni 30 minuti circa
(`SMARTLEX24_SESSION_TTL`); le ricerche e le letture usano poi semplici
richieste HTTP dirette.

Conseguenza pratica: la **prima** richiesta di ogni sessione è lenta, le
successive sono rapide finché il token resta valido.

## 1. Installazione locale

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    playwright install --with-deps chromium

## 2. Credenziali

Variabili d'ambiente (mai nel codice, mai nel repository):

    export SMARTLEX24_USERNAME="tuo_username"
    export SMARTLEX24_PASSWORD="tua_password"

## 3. Prove locali

Test del parsing (senza rete né credenziali):

    python test_parsing.py

Ricerca reale da riga di comando:

    python server.py --probe "licenziamento illegittimo"

## 4. Collegamento a Claude

**Remoto (Render)**: si distribuisce con **Docker** (c'è un `Dockerfile`
basato sull'immagine ufficiale di Playwright, che contiene già Chromium).

Su Render, nel Web Service:
- **Runtime**: Docker
- Build/Start Command: non servono (sono nel `Dockerfile`)
- Variabili d'ambiente:
  - `MCP_PATH` = un percorso segreto, es. `/k7f2-9q1x/mcp` (**obbligatorio
    in pratica**: senza, chiunque conosca l'indirizzo del server può usare il
    tuo abbonamento; il server stampa un avviso all'avvio se manca)
  - `SMARTLEX24_USERNAME`, `SMARTLEX24_PASSWORD`
  - `PORT` è gestita da Render

Chromium headless consuma parecchia RAM: sul piano gratuito il login può
fallire per mancanza di memoria (timeout) e il servizio si addormenta dopo
un periodo di inattività (la prima richiesta dopo il risveglio è lenta).

Poi in Claude: Impostazioni -> Connettori -> Aggiungi connettore
personalizzato -> URL `https://tuo-host/percorso-segreto/mcp`.

**Locale (solo Claude Desktop)**: in `claude_desktop_config.json`:

    {"mcpServers": {"smartlex24": {
      "command": "/percorso/.venv/bin/python",
      "args": ["/percorso/server.py"],
      "env": {"MCP_TRANSPORT": "stdio",
               "SMARTLEX24_USERNAME": "...",
               "SMARTLEX24_PASSWORD": "..."}}}}

## Diagnostica

Per abilitare temporaneamente `diagnostica_login`, imposta
`SMARTLEX24_ABILITA_DIAGNOSTICA=1` su Render e riavvia; toglila finito il
debug.

## Note d'uso e limiti noti

- Se cambi password sul sito, aggiorna `SMARTLEX24_PASSWORD` e riavvia.
- I selettori del form di login si possono correggere via variabili
  d'ambiente senza toccare il codice (vedi `config.py`).
- La ricerca non ha filtri (famiglia, autorità, anno): parole come
  "Cassazione" nella query non restringono i risultati.
- L'URL dei PDF, quando il sito non lo fornisce, è ricostruito dallo schema
  osservato (`/dda/{ultima cifra dell'id}/{id}.PDF`): segnalato con
  `url_pdf_dedotto: true`.
- PDF scansionati (immagini) non hanno testo estraibile: viene segnalato.
- In un caso su due verificati, la `data` della ricerca non coincideva con
  la data di deposito della sentenza: da verificare se è un metadato del
  sito.
- Non è stato verificato cosa succede in caso di 2FA/captcha al login: se il
  sito li introducesse, il login automatico fallirebbe.
