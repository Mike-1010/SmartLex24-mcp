"""Configurazione del connettore SmartLex24 (Il Sole 24 Ore).
Modifica qui, non in server.py."""
import os

BASE = "https://smartlex24.ilsole24ore.com"
# "?logout=true" forza la comparsa del form di login anche se il browser
# (nuovo, ad ogni sessione Playwright) avesse residui di sessione.
LOGIN_PAGE_URL = f"{BASE}/public/default.aspx?logout=true"

# Vero endpoint di ricerca (Apache/IIS REST, non ASP.NET WebForms classico):
# scoperto via DevTools il 28/09/2026.
SEARCH_URL = "https://dwa.ilsole24ore.com/dir/api/BD.Search.BDSearchServiceREST.svc/PullSearch3"
TOKEN_INFO_URL = "https://dwa.ilsole24ore.com/dir/api/BD.User.BDUserServiceREST.svc/GetTokenInfo"

# Credenziali: MAI hardcoded. Impostale come variabili d'ambiente su Render.
SMARTLEX24_USERNAME = os.getenv("SMARTLEX24_USERNAME", "")
SMARTLEX24_PASSWORD = os.getenv("SMARTLEX24_PASSWORD", "")

# --- Selettori del form di login, verificati via DevTools il 28/09/2026 ---
# È un form HTML classico (non una chiamata AJAX):
#   <form id="login" action="https://du.ilsole24ore.com/utenti/authfiles/logincentrale.aspx" method="post">
#     <input type="hidden" name="realSubmit" value="BYPASS">
#     <input type="hidden" name="RURL" value="https://smartlex24.ilsole24ore.com/public/default.aspx">
#     <input type="hidden" name="ERRURL" value="https://smartlex24.ilsole24ore.com/public/default.aspx">
#     <input type="hidden" name="SC" value="CO">
#     <input type="hidden" name="RememberMe" value="1">
#     <input type="text" id="log_user" name="txtUsername">
#     <input type="password" id="log_pass" name="txtPassword">
#   </form>
# Impostabili anche da variabile d'ambiente, per poterli correggere senza
# dover ridistribuire il codice se il sito cambia leggermente il markup.
LOGIN_USERNAME_SELECTOR = os.getenv("SMARTLEX24_LOGIN_USERNAME_SELECTOR", "#log_user")
LOGIN_PASSWORD_SELECTOR = os.getenv("SMARTLEX24_LOGIN_PASSWORD_SELECTOR", "#log_pass")
LOGIN_SUBMIT_SELECTOR = os.getenv("SMARTLEX24_LOGIN_SUBMIT_SELECTOR", "#login button[type=submit]")
LOGIN_ERROR_SELECTOR = os.getenv("SMARTLEX24_LOGIN_ERROR_SELECTOR", "#loginErrorMessageClient")

# Banner cookie (OneTrust) che compare sopra il form e intercetta i click sul
# bottone "Accedi" (scoperto al primo test reale, 28/09/2026): va chiuso
# prima di cliccare submit. Bottone "Rifiuta tutto" (non "Accetta tutto",
# per non lasciare tracce di consenso non necessarie), con fallback ad
# "Accetta tutto" se il primo non è presente sulla pagina.
COOKIE_REJECT_SELECTOR = os.getenv("SMARTLEX24_COOKIE_REJECT_SELECTOR", "#onetrust-reject-all-handler")
COOKIE_ACCEPT_SELECTOR = os.getenv("SMARTLEX24_COOKIE_ACCEPT_SELECTOR", "#onetrust-accept-btn-handler")
COOKIE_BANNER_WAIT_MS = int(os.getenv("SMARTLEX24_COOKIE_BANNER_WAIT_MS", "3000"))

# Timeout e attese (il sito è una SPA pesante, con molte chiamate xhr in sequenza)
NAV_TIMEOUT_MS = int(os.getenv("SMARTLEX24_NAV_TIMEOUT_MS", "45000"))
LOGIN_WAIT_MS = int(os.getenv("SMARTLEX24_LOGIN_WAIT_MS", "8000"))

# Sessione: il token opaco (campo "token" nel payload delle chiamate REST)
# viene rigenerato ad ogni login via browser; lo teniamo in cache in memoria
# per evitare di rifare login ad ogni ricerca.
SESSION_TTL = int(os.getenv("SMARTLEX24_SESSION_TTL", "1800"))  # secondi

TIMEOUT = 30
DEFAULT_ROWS = 10
MAX_ROWS = 20
