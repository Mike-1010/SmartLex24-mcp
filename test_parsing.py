"""Test delle funzioni di parsing, senza rete né credenziali.

Le fixture riproducono i risultati reali osservati il 03/11/2026 durante i
test dal vivo del connettore. Esecuzione:  python test_parsing.py
"""
import asyncio

import config
import server

SCHEDA_ARTICOLO = {
    "idDocumento": 28190,
    "famiglia": "Legge e Prassi",
    "rank": 0.0,
    "visibile": True,
    "abstract": '<h3 class="ncard-title">Articolo  2043</h3>'
                '<p class="ncard-subtitle">Risarcimento per fatto illecito</p> ',
}

SCHEDA_PDF = {
    "idDocumento": 44883625,
    "tipologia": "152/Giurisprudenza/Corte di Cassazione civile/Sezione 3",
    "data": "2026-03-02T00:00:00Z",
    "famigliaCode": "152/Giurisprudenza",
    "url": "https://static.ilsole24ore.com/dda/5/44883625.PDF",
    "rank": 2113.959,
    "visibile": False,
    "abstract": '<h3 class="ncard-title">Integrale in PDF</h3>'
                '<p class="ncard-subtitle hideInListingHead">Responsabilità civile - '
                'Responsabilità medica - Nozione di complicanza - CTU</p>',
}

SENTENZA = {
    "idDocumento": 45464265,
    "tipologia": "152/Giurisprudenza/Corte di Cassazione civile/Sezione 3",
    "data": "2026-05-24T00:00:00Z",
    "famigliaCode": "152/Giurisprudenza",
    "rank": 3811.98,
    "visibile": False,
    "abstract": "1. Il presente giudizio &egrave; stato promosso dinanzi al\r\nTribunale di Napoli...",
}

SCHEDA_NORMA_VUOTA = {"famiglia": "Legge e Prassi", "rank": 0.0, "visibile": True, "abstract": "",
                      "idDocumento": 0, "idProvvedimento": 7781, "text1": "<b>Regolamento (UE) n. 1215/2012</b>",
                      "RelatedDocuments": [], "nascondi": False}

SCHEDA_NORMA_CON_FIGLI = {
    "famiglia": "Legge e Prassi",
    "rank": 0.0,
    "Gruppo": {"Documenti": [{"idDocumento": 111, "titolo": "Reg. UE 1215/2012 - art. 1"},
                             {"idDocumento": 112, "titolo": "Reg. UE 1215/2012 - art. 2"}]},
}


def test_scheda_articolo():
    r = server._normalizza_documento(SCHEDA_ARTICOLO)
    assert r["titolo"] == "Articolo 2043 - Risarcimento per fatto illecito", r["titolo"]
    assert r["estratto"] == ""
    assert r["apribile"] is True
    assert "visibile" not in r


def test_scheda_pdf():
    r = server._normalizza_documento(SCHEDA_PDF)
    assert r["titolo"].startswith("Responsabilità civile"), r["titolo"]
    assert r["formato"] == "pdf"
    assert r["famiglia"] == "Giurisprudenza"
    assert r["tipologia"] == "Giurisprudenza/Corte di Cassazione civile/Sezione 3"
    assert r["intestazione"] == "Corte di Cassazione civile, Sezione 3 - 02/03/2026", r["intestazione"]
    assert "<" not in r["estratto"]


def test_sentenza_senza_titolo():
    r = server._normalizza_documento(SENTENZA)
    assert r["titolo"] is None
    assert r["intestazione"] == "Corte di Cassazione civile, Sezione 3 - 24/05/2026"
    assert r["data"] == "2026-05-24"
    assert "è stato promosso" in r["estratto"] and "\r" not in r["estratto"]
    assert r["formato"] is None


def test_scheda_norma_senza_id():
    docs = server._appiattisci([SCHEDA_NORMA_VUOTA, SENTENZA])
    assert len(docs) == 2
    r = server._normalizza_documento(docs[0])
    assert r["apribile"] is False and r["idDocumento"] is None
    assert r["anteprima_campi"] == {"idProvvedimento": 7781, "text1": "Regolamento (UE) n. 1215/2012",
                                    "RelatedDocuments_numero": 0}, r["anteprima_campi"]


def test_scheda_norma_con_documenti_annidati():
    docs = server._appiattisci([SCHEDA_NORMA_CON_FIGLI, SENTENZA])
    assert [server._id_di(d) for d in docs] == [111, 112, 45464265]
    r = server._normalizza_documento(docs[0])
    assert r["titolo"] == "Reg. UE 1215/2012 - art. 1" and r["apribile"] is True


def test_date_vuote():
    assert server._data_iso("0001-01-01T00:00:00") is None
    assert server._data_iso(None) is None


def test_url_pdf():
    assert server._url_pdf_dedotto("44883625") == "https://static.ilsole24ore.com/dda/5/44883625.PDF"
    assert server._url_pdf_dedotto("40164153").endswith("/3/40164153.PDF")
    trovato = server._trova_url_pdf({"Allegati": [{"Link": "vedi https://static.ilsole24ore.com/dda/9/45966459.PDF ora"}]})
    assert trovato == "https://static.ilsole24ore.com/dda/9/45966459.PDF"
    assert server._host_consentito("https://static.ilsole24ore.com/dda/5/1.PDF")
    assert not server._host_consentito("http://static.ilsole24ore.com/dda/5/1.PDF")
    assert not server._host_consentito("https://ilsole24ore.com.evil.example/x.pdf")
    assert not server._host_consentito("https://evil.example/ilsole24ore.com.pdf")


def test_validazione_id():
    for cattivo in ("abc'; DROP", "", "12a", "1" * 13):
        r = asyncio.run(server._get_document(cattivo))
        assert "non valido" in r["errore"], (cattivo, r)


def test_query_vuota():
    assert "vuota" in asyncio.run(server._search("   ", 5))["errore"]


def test_diagnostica_disattivata():
    nomi = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert nomi == ["cerca_smartlex24", "leggi_documento_smartlex24"], nomi
    assert config.ABILITA_DIAGNOSTICA is False


def test_titolo_norma_con_vigenza():
    text2 = ("<p>LIBRO QUARTO. Delle obbligazioni - TITOLO NONO. Dei fatti illeciti</p>"
             "<h1>Articolo 2043 Risarcimento per fatto illecito</h1><p>Testo in vigore dal 21 aprile 1942</p>")
    t = server._ultimo_titolo_da_html(text2)
    assert "Articolo 2043" in t and "in vigore" in t, t
    sentenza = "<p>Data udienza 4 marzo 2026</p><p>Integrale</p><p>Responsabilità medica - Consenso informato</p>"
    assert server._ultimo_titolo_da_html(sentenza) == "Responsabilità medica - Consenso informato"


def test_intestazioni_ripetute_pdf():
    calce = "Firmato Da: EMILIO IANNELLO Emesso Da: CA\nData pubblicazione 02/03/2026"
    pagine = [f"Testo pagina {i}\n{i}\n{calce}" for i in range(1, 6)]
    out = "\n".join(server._togli_intestazioni_ripetute(pagine))
    assert out.count("Data pubblicazione 02/03/2026") == 1, out
    assert all(f"Testo pagina {i}" in out for i in range(1, 6))
    assert "\n3\n" not in out


def test_gemello_html():
    import json
    import httpx
    titolo = "<p>Data udienza 28 gennaio 2026</p><p>Integrale</p><p>Resp. medica - CTU</p>"
    documenti = {
        "44883625": {"DocumentId": 44883625, "Text2": "<p>Integrale in PDF</p><p>Resp. medica - CTU</p>",
                     "TestoDoc": "", "Blocked": False, "DocumentIdNext": 44890439, "DocumentIdPrev": 44000001},
        "44890439": {"DocumentId": 44890439, "Text2": titolo, "TestoDoc": "<p>Si.Gi. ricorre</p>", "Blocked": False},
        "44000001": {"DocumentId": 44000001, "Text2": "<p>Altro</p>", "TestoDoc": "<p>altro</p>", "Blocked": False},
    }

    def handler(req):
        return httpx.Response(200, json={"Result": documenti[json.loads(req.content)["documentId"]]})

    originale_client, originale_sessione = httpx.AsyncClient, server.ensure_session

    async def finta():
        return "tok"
    server.httpx.AsyncClient = lambda **kw: originale_client(transport=httpx.MockTransport(handler), **kw)
    server.ensure_session = finta
    try:
        r = asyncio.run(server._get_document("44883625"))
    finally:
        server.httpx.AsyncClient = originale_client
        server.ensure_session = originale_sessione
    assert r["idDocumento"] == 44890439 and r["id_richiesto"] == 44883625, r
    assert r["testo_integrale"] == "Si.Gi. ricorre" and r["formato"] == "html"
    assert r["url_pdf"].endswith("/5/44883625.PDF")


def test_estrazione_pdf():
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        print("  (reportlab non installato: test estrazione PDF saltato)")
        return
    import io
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "REPUBBLICA ITALIANA - LA CORTE SUPREMA DI CASSAZIONE")
    c.showPage()
    c.drawString(72, 720, "P.Q.M. Rigetta il ricorso.")
    c.save()
    testo = server._estrai_testo_pdf(buf.getvalue())
    assert "CORTE SUPREMA DI CASSAZIONE" in testo and "Rigetta il ricorso" in testo, testo


if __name__ == "__main__":
    test = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in test:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(test)} test superati.")
