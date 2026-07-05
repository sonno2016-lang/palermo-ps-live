#!/usr/bin/env python3
"""
Scraper leggero per gli accessi ai Pronto Soccorso di Palermo.
By Dry

Nessun database, nessuna coda, nessuna cache: gira in un colpo solo e stampa/salva JSON.

Dipendenze:
    pip install requests beautifulsoup4

Uso:
    python palermo_ps_scraper.py            # stampa tutto a schermo
    python palermo_ps_scraper.py --json out.json   # salva anche su file
"""

import json
import re
import sys
import time
import argparse
import requests
import urllib3
from bs4 import BeautifulSoup

# Le richieste usano verify=False perché alcuni siti PA hanno certificati
# scaduti/self-signed. Disabilitiamo qui il warning una volta sola, in modo
# esplicito, invece di farlo sparire silenziosamente ad ogni richiesta.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS_DEFAULT = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/100.0.4896.79 Safari/537.36"
}

TIMEOUT = 12
RATE_LIMIT_SECONDS = 1.5  # pausa tra una fonte e l'altra, per non martellare i siti


def _tutti_none(d):
    """True se un dizionario (anche annidato) non contiene nessun valore utile."""
    if d is None:
        return True
    if isinstance(d, dict):
        return all(_tutti_none(v) for v in d.values())
    if isinstance(d, list):
        return all(_tutti_none(v) for v in d)
    return False


# ---------------------------------------------------------------------------
# 1) INGRASSIA (ASP Palermo) - fonte più semplice: HTML statico, no robots.txt
# ---------------------------------------------------------------------------
def get_ingrassia():
    """
    Pagina: https://www.asppalermo.org/attese_ps/index_mod2.php
    Struttura: più blocchi div (uno per presidio), ognuno contenente una
    tabella con colonne = colori triage e righe = stato paziente.
    Per Ingrassia il blocco è il div #1 (vedi 'replaceTo' => [1] nel config originale).
    """
    url = "https://www.asppalermo.org/attese_ps/index_mod2.php"
    r = requests.get(url, headers=HEADERS_DEFAULT, timeout=TIMEOUT, verify=False)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    return _parse_asp_block(soup, div_index=1, nome="Palermo - Ingrassia")


def _parse_asp_block(soup, div_index: int, nome: str):
    """
    Traduzione di $dataCommonAspPalermo (vedi palermo.php righe 61-199).
    Colonne: td2=rosso, td3=arancione, td4=verde, td5=bianco
    Righe:   tr1=in_attesa, tr2=in_trattamento, tr3=in_osservazione, tr4=totale_colore
    """
    def cell(row, col):
        sel = f"div:nth-of-type({div_index}) table tbody tr:nth-of-type({row}) td:nth-of-type({col})"
        el = soup.select_one(sel)
        if not el:
            return None
        txt = el.get_text(strip=True)
        digits = re.sub(r"[^0-9]", "", txt)
        return int(digits) if digits != "" else None

    colori = {"rosso": 2, "arancione": 3, "verde": 4, "bianco": 5}
    righe = {"in_attesa": 1, "in_trattamento": 2, "in_osservazione": 3, "totale": 4}

    out = {"nome": nome, "data": {}}
    somma = {"in_attesa": 0, "in_trattamento": 0, "in_osservazione": 0}

    for colore, col in colori.items():
        dettaglio = {campo: cell(riga, col) for campo, riga in righe.items()}
        out["data"][colore] = dettaglio
        for k in somma:
            somma[k] += dettaglio.get(k) or 0

    # indice di sovraffollamento e posti tecnici (extra a livello di struttura)
    extra_sel = f"div:nth-of-type({div_index}) div:nth-of-type(2) span"
    spans = soup.select(extra_sel)
    out["extra"] = {
        "numero_posti_tecnico": spans[2].get_text(strip=True) if len(spans) > 2 else None,
        "indice_sovraffollamento": spans[4].get_text(strip=True) if len(spans) > 4 else None,
    }
    out["totali"] = somma
    return out


# ---------------------------------------------------------------------------
# 2) BUCCHERI LA FERLA - endpoint JSON interno (niente HTML da parsare)
# ---------------------------------------------------------------------------
def get_buccheri():
    """
    Il sito carica i dati via AJAX POST da:
    https://servizionline.provinciaromanafbf.it/palermo/monitorps/ajax/dati-monitor-ps/01/G
    """
    url = "https://servizionline.provinciaromanafbf.it/palermo/monitorps/ajax/dati-monitor-ps/01/G"
    headers = {
        **HEADERS_DEFAULT,
        "Referer": "https://servizionline.provinciaromanafbf.it/palermo/monitorps/portal-view/01/G",
        "Origin": "https://servizionline.provinciaromanafbf.it",
    }
    r = requests.post(url, headers=headers, timeout=TIMEOUT, verify=False)
    r.raise_for_status()
    dati = r.json()

    def g(path, default=None):
        cur = dati
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    return {
        "nome": "Palermo - Buccheri La Ferla",
        "data": {
            "rosso": {
                "in_attesa": g("pazientiInAttesa.totaleCodiciRossi"),
                "in_trattamento": g("pazientiInTrattamento.totaleCodiciRossi"),
            },
            "giallo": {
                "in_attesa": g("pazientiInAttesa.totaleCodiciGialli"),
                "in_trattamento": g("pazientiInTrattamento.totaleCodiciGialli"),
            },
            "verde": {
                "in_attesa": g("pazientiInAttesa.totaleCodiciVerdi"),
                "in_trattamento": g("pazientiInTrattamento.totaleCodiciVerdi"),
            },
            "bianco": {
                "in_attesa": g("pazientiInAttesa.totaleCodiciBianchi"),
                "in_trattamento": g("pazientiInTrattamento.totaleCodiciBianchi"),
            },
        },
        "totali": {"pazienti": g("pazientiInAttesa.totalePazienti")},
        "extra": {
            "accessi_giorno": g("numeroAccessiProntoSoccorsoOdierno"),
            "accessi_anno": g("numeroAccessiProntoSoccorsoAnno"),
            "indice_sovraffollamento": g("indicatoreSovraffollamento.valore"),
        },
    }


# ---------------------------------------------------------------------------
# 3) POLICLINICO - due endpoint JSON REST
# ---------------------------------------------------------------------------
def get_policlinico():
    base_headers = {
        **HEADERS_DEFAULT,
        "Referer": "https://www.policlinico.pa.it/web/guest",
        "Origin": "https://www.policlinico.pa.it/web/guest",
    }
    url_ps = "https://www.policlinico.pa.it/o/PoliclinicoPaRestBuilder/v1.0/ProntoSoccorso"
    url_idx = "https://www.policlinico.pa.it/o/PoliclinicoPaRestBuilder/v1.0/ProntoSoccorsoIndici"

    r1 = requests.get(url_ps, headers=base_headers, timeout=TIMEOUT, verify=False)
    r2 = requests.get(url_idx, headers=base_headers, timeout=TIMEOUT, verify=False)
    r1.raise_for_status()
    r2.raise_for_status()
    dati, indici = r1.json(), r2.json()

    def g(d, path, default=None):
        cur = d
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    colori = {
        "rosso": "Rosso (1)", "arancione": "Arancione (2)", "azzurro": "Azzurro (3)",
        "verde": "Verde (4)", "bianco": "Bianco (5)",
    }

    data = {}
    tot_attesa = tot_trattamento = 0
    for nome_colore, chiave in colori.items():
        att = g(dati, f"pazientiInAttesa.{chiave}", 0) or 0
        tratt = g(dati, f"pazientiInCarico.{chiave}", 0) or 0
        data[nome_colore] = {
            "in_attesa": att,
            "in_trattamento": tratt,
            "carichi_urgenti": g(dati, f"carichiUrgenza.{chiave}"),
            "tempo_attesa": g(dati, f"tempiMediAttesa.{chiave}"),
        }
        tot_attesa += att if isinstance(att, (int, float)) else 0
        tot_trattamento += tratt if isinstance(tratt, (int, float)) else 0

    def num(v):
        digits = re.sub(r"[^0-9]", "", str(v)) if v is not None else ""
        return int(digits) if digits != "" else 0

    p24 = num(indici.get("permanenza24H"))
    p2448 = num(indici.get("permanenza2448H"))
    pOltre = num(indici.get("permanenzaOltre24H"))
    postiTecnici = num(indici.get("postiTecniciPresidiati")) or 1
    indice_sovraffollamento = round((p24 + p2448 + pOltre) / postiTecnici * 100, 2)

    return {
        "nome": "Palermo - Policlinico",
        "data": data,
        "totali": {"in_attesa": tot_attesa, "in_trattamento": tot_trattamento},
        "extra": {
            "permanenza_24h": p24,
            "permanenza_24_48h": p2448,
            "permanenza_oltre_24h": pOltre,
            "posti_tecnici_presidiati": postiTecnici,
            "indice_sovraffollamento": indice_sovraffollamento,
        },
    }


# ---------------------------------------------------------------------------
# 4) VILLA SOFIA / CERVELLO - scraping CSS (sito con robots.txt restrittivo)
# ---------------------------------------------------------------------------
def get_ospedali_riuniti(presidio: str):
    """
    presidio: 'villasofia' oppure 'cervello'
    ATTENZIONE: questo dominio dichiara robots.txt che disallow l'accesso
    automatico. Il progetto originale lo fa comunque; qui la logica è
    tradotta fedelmente dal config PHP ma NON testata dal mio ambiente
    (nessun accesso di rete a questo dominio dal sandbox in cui giro).
    Verificala tu prima di usarla in modo continuativo.
    """
    url = "https://www.ospedaliriunitipalermo.it/amministrazione-trasparente/servizi-erogati/liste-di-attesa/pazienti-in-attesa-al-pronto-soccorso/"
    headers = {
        **HEADERS_DEFAULT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.ospedaliriunitipalermo.it/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    r = requests.get(url, headers=headers, timeout=TIMEOUT, verify=False)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # indice del blocco: villa sofia = 4, cervello = 3 (da config originale)
    blocco = 4 if presidio == "villasofia" else 3

    def cell(riga, col):
        sel = (f"div.olo-container-pronto-soccorso > div:nth-of-type({blocco}) "
               f"> div:nth-of-type(4) > div:nth-of-type({riga}) > div:nth-of-type({col})")
        el = soup.select_one(sel)
        if not el:
            return None
        digits = re.sub(r"[^0-9]", "", el.get_text(strip=True))
        return int(digits) if digits != "" else None

    colori_ordine = ["rosso", "giallo", "verde", "bianco"]
    data = {colore: cell(i + 1, 2) for i, colore in enumerate(colori_ordine)}

    if _tutti_none(data):
        raise ValueError(
            "Nessun valore estratto: i selettori CSS potrebbero essere cambiati "
            "o la struttura della pagina è diversa da quella attesa. "
            "Verifica manualmente i selettori prima di fidarti di questa fonte."
        )

    return {
        "nome": f"Palermo - {'Villa Sofia' if presidio == 'villasofia' else 'Cervello'}",
        "data": data,
        "nota": "Selettori tradotti dal config originale, non verificati per robots.txt/accesso rete.",
    }


# ---------------------------------------------------------------------------
# 5) CIVICO - tabella HTML + regex su testo libero (sito con robots.txt restrittivo)
# ---------------------------------------------------------------------------
def get_civico():
    """
    ATTENZIONE: stesso discorso di sopra, arnascivico.it disallow robots.
    Logica tradotta da ArsCivicoJob.php (parsing tabella + regex su paragrafo).
    Non verificata dal mio ambiente.
    """
    url = "https://www.arnascivico.it/index.php?option=com_content&view=article&id=3415&catid=24&Itemid=139"
    headers = {
        **HEADERS_DEFAULT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.arnascivico.it/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    r = requests.get(url, headers=headers, timeout=TIMEOUT, verify=False)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    tabella = soup.select_one("table.gridtable")
    righe_out = {}
    if tabella:
        trs = tabella.select("tbody tr") or tabella.select("tr")
        for tr in trs[1:]:  # salta l'header
            celle = tr.find_all(["td", "th"])
            if not celle:
                continue
            etichetta = celle[0].get_text(strip=True).lower().replace("totale", "totali")
            valori = [c.get_text(strip=True) for c in celle[1:]]
            righe_out[etichetta] = valori

    # testo libero con indice di sovraffollamento e permanenze (via regex)
    testo_completo = soup.get_text(" ", strip=True)
    pattern = {
        "indice_sovraffollamento": r"Indice Sovraffollamento:\s*(\d+)%",
        "permanenza_minore_24h": r"permanenza\s*<?\s*24h:\s*(\d+)",
        "permanenza_24_48h": r"permanenza compresa tra 24h e 48h:\s*(\d+)",
        "permanenza_oltre_48h": r"permanenza\s*>?\s*48h:\s*(\d+)",
    }
    extra = {}
    for chiave, regex in pattern.items():
        m = re.search(regex, testo_completo, re.IGNORECASE)
        extra[chiave] = m.group(1) if m else None

    if _tutti_none(righe_out) and _tutti_none(extra):
        raise ValueError(
            "Nessun valore estratto: né la tabella né il testo libero hanno prodotto dati. "
            "I selettori/regex potrebbero non corrispondere più alla pagina attuale."
        )

    return {
        "nome": "Palermo - Civico",
        "data": righe_out,
        "extra": extra,
        "nota": "Parsing tabella tradotto da ArsCivicoJob.php, non verificato per robots.txt/accesso rete.",
    }


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
FONTI = {
    "ingrassia": get_ingrassia,
    "buccheri": get_buccheri,
    "policlinico": get_policlinico,
    "villasofia": lambda: get_ospedali_riuniti("villasofia"),
    "cervello": lambda: get_ospedali_riuniti("cervello"),
    "civico": get_civico,
}


def main():
    parser = argparse.ArgumentParser(description="Scraper accessi PS Palermo")
    parser.add_argument("--json", help="salva il risultato anche su file JSON")
    parser.add_argument(
        "--solo", nargs="*", choices=list(FONTI.keys()),
        help="limita lo scraping solo a certe fonti (default: tutte)"
    )
    args = parser.parse_args()

    fonti_da_eseguire = args.solo or list(FONTI.keys())
    risultati = {}

    for i, nome_fonte in enumerate(fonti_da_eseguire):
        try:
            risultati[nome_fonte] = FONTI[nome_fonte]()
        except Exception as e:
            risultati[nome_fonte] = {"errore": str(e)}
        if i < len(fonti_da_eseguire) - 1:
            time.sleep(RATE_LIMIT_SECONDS)

    output = json.dumps(risultati, ensure_ascii=False, indent=2)
    print(output)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(output)


if __name__ == "__main__":
    main()
