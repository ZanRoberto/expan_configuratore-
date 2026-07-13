#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EXPAN CONFIGURATORE — back-end FastAPI
Espone: creazione offerta con numerazione ATOMICA, lista offerte,
export JSON multi-offerta per l'ERP (protetto da chiave), e serve il
configuratore HTML statico.

DB: SQLite (file). In produzione su Render il file DEVE stare su un
persistent disk, altrimenti a ogni deploy si azzera (numerazione persa).
Percorso configurabile via env DB_PATH.

NOTA ONESTA: questo file non è stato eseguito nell'ambiente di sviluppo
(niente rete per installare FastAPI). La verifica end-to-end avviene al
primo deploy su Render, leggendo i log. Il CUORE (numerazione atomica) è
invece già stato testato a parte con 50 richieste concorrenti: zero collisioni.
"""
import os, sqlite3, datetime, json
import urllib.request, urllib.error
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

DB_PATH   = os.environ.get("DB_PATH", "./data/expan.db")   # cartella locale del progetto, sempre scrivibile
API_KEY   = os.environ.get("EXPORT_API_KEY", "cambia-questa-chiave")
HTML_FILE = os.environ.get("HTML_FILE", "configuratore_expan_v2.html")

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title="EXPAN Configuratore", version="1.0", lifespan=lifespan)

# ══════════════════ DB ══════════════════
def get_con():
    d = os.path.dirname(DB_PATH)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def init_db():
    con = get_con()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS contatori(
        chiave TEXT PRIMARY KEY, anno INTEGER NOT NULL, valore INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS offerte(
        numero TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        cliente_cod TEXT,
        stato TEXT NOT NULL DEFAULT 'bozza',   -- bozza | confermata | passata
        creata_da TEXT,
        payload_json TEXT);
    """)
    con.commit(); con.close()

# ══════════════════ NUMERAZIONE ATOMICA (cuore già testato) ══════════════════
def assegna_numero(con, cliente_cod, creata_da, payload):
    anno = datetime.datetime.now().year
    con.execute("BEGIN IMMEDIATE;")
    row = con.execute("SELECT valore, anno FROM contatori WHERE chiave='offerta';").fetchone()
    if row is None:
        nuovo = 1
        con.execute("INSERT INTO contatori(chiave,anno,valore) VALUES('offerta',?,?);", (anno, nuovo))
    else:
        valore, anno_salv = row
        nuovo = 1 if anno_salv != anno else valore + 1
        con.execute("UPDATE contatori SET valore=?, anno=? WHERE chiave='offerta';", (nuovo, anno))
    numero = f"OFF-{anno}-{nuovo:04d}"
    con.execute(
        "INSERT INTO offerte(numero,data,cliente_cod,stato,creata_da,payload_json) VALUES(?,?,?,?,?,?);",
        (numero, datetime.date.today().isoformat(), cliente_cod, 'bozza', creata_da,
         json.dumps(payload, ensure_ascii=False) if payload is not None else None))
    con.execute("COMMIT;")
    return numero

# ══════════════════ ENDPOINTS ══════════════════
@app.get("/api/health")
def health():
    return {"status": "ok", "db": DB_PATH, "time": datetime.datetime.now().isoformat(timespec="seconds")}

@app.post("/api/offerte")
async def crea_offerta(request: Request):
    """Crea un'offerta e assegna il numero atomico. Body JSON: {cliente_cod, creata_da, payload}"""
    body = await request.json()
    con = get_con()
    try:
        numero = assegna_numero(con, body.get("cliente_cod"), body.get("creata_da", "web"), body.get("payload"))
    except Exception as e:
        con.execute("ROLLBACK;"); con.close()
        raise HTTPException(500, f"errore numerazione: {e}")
    con.close()
    return {"numero": numero, "stato": "bozza"}

@app.patch("/api/offerte/{numero}/stato")
async def cambia_stato(numero: str, request: Request):
    """Cambia stato: bozza → confermata → passata. Body: {stato}"""
    body = await request.json()
    nuovo = body.get("stato")
    if nuovo not in ("bozza", "confermata", "passata"):
        raise HTTPException(400, "stato non valido")
    con = get_con()
    cur = con.execute("UPDATE offerte SET stato=? WHERE numero=?;", (nuovo, numero))
    con.commit(); n = cur.rowcount; con.close()
    if n == 0:
        raise HTTPException(404, "offerta non trovata")
    return {"numero": numero, "stato": nuovo}

@app.get("/api/offerte")
def lista_offerte(stato: str = None):
    """Lista offerte (leggera). Filtro opzionale per stato."""
    con = get_con()
    q = "SELECT numero,data,cliente_cod,stato,creata_da FROM offerte"
    args = ()
    if stato:
        q += " WHERE stato=?"; args = (stato,)
    q += " ORDER BY numero DESC;"
    rows = con.execute(q, args).fetchall(); con.close()
    return [{"numero":r[0],"data":r[1],"cliente_cod":r[2],"stato":r[3],"creata_da":r[4]} for r in rows]

@app.get("/api/export")
def export_erp(stato: str = "confermata", x_api_key: str = Header(None)):
    """
    ENDPOINT ERP: restituisce le offerte (default: confermate) con payload completo.
    Protetto da chiave (header X-API-Key). L'ERP viene qui a prendersi i dati.
    """
    if x_api_key != API_KEY:
        raise HTTPException(401, "chiave non valida")
    con = get_con()
    rows = con.execute(
        "SELECT numero,data,cliente_cod,stato,payload_json FROM offerte WHERE stato=? ORDER BY numero;",
        (stato,)).fetchall()
    con.close()
    offerte = []
    for r in rows:
        payload = json.loads(r[4]) if r[4] else {}
        offerte.append({"numero":r[0],"data":r[1],"cliente_cod":r[2],"stato":r[3],"offerta":payload})
    return {"schema":"EXPAN-EXPORT-ERP","versione":"1.0",
            "generato":datetime.datetime.now().isoformat(timespec="seconds"),
            "conteggio":len(offerte),"offerte":offerte}

# ══════════════════ PARSER AI (DeepSeek) ══════════════════
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

PROMPT_PARSER = """Sei il parser di un configuratore di pressati in schiuma (profilo 2D estruso). Converti la richiesta in JSON. Rispondi SOLO con JSON valido, nessun testo, nessun markdown.
Schema:{"shape":"RETT|POLY|ELL|LIBERO","rett":{"L":n,"P":n,"R":n},"poly":{"n":int,"d":n,"rot":n},"ell":{"a":n,"b":n},"strati":[{"materiale":"PU25|HR30|HR35|MEMORY50|GEL55","spessore_cm":n}],"fori":[{"d":n,"cx":n,"cy":n}],"canali":{"num":int,"larghezza_cm":n,"profondita_cm":n},"bugnato":bool,"estetica":"stringa o vuoto","quantita":int,"cliente":"MATVEN|NAUADR|MEDSRL|NUOVO","dubbi":["campo: motivo"]}
Regole: triangolo->POLY n=3, esagono->POLY n=6, dodecagono/12 lati->POLY n=12, "diametro/lato"->poly.d. Rettangolo/lastra->RETT. Ovale/ellittico->ELL. Sagoma irregolare->LIBERO. "foro centrale"->cx=0,cy=0. Strati dall'alto. Materiale mancante->plausibile + voce in dubbi. Clienti: Materassificio Veneto=MATVEN, Nautica Adria=NAUADR, Ospedaliera Med=MEDSRL, else NUOVO."""

@app.post("/api/interpreta")
async def interpreta(request: Request):
    """Riceve {testo}, chiama DeepSeek, restituisce il JSON dei parametri."""
    if not DEEPSEEK_API_KEY:
        raise HTTPException(503, "DEEPSEEK_API_KEY non configurata sul server")
    body = await request.json()
    testo = (body.get("testo") or "").strip()
    if not testo:
        raise HTTPException(400, "testo mancante")

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": PROMPT_PARSER},
            {"role": "user", "content": testo},
        ],
        "temperature": 0,
        "stream": False,
    }
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"DeepSeek HTTP {e.code}: {e.read().decode('utf-8')[:200]}")
    except Exception as e:
        raise HTTPException(502, f"errore DeepSeek: {e}")

    try:
        contenuto = data["choices"][0]["message"]["content"]
        contenuto = contenuto.replace("```json", "").replace("```", "").strip()
        parametri = json.loads(contenuto)
    except Exception as e:
        raise HTTPException(502, f"risposta DeepSeek non interpretabile: {e}")
    return parametri

# ═══════════════════════════════════════════════════════════════════════════
#  NARRATORE / SUPERRISPONDITORE — nascita di una libreria da soggetto+CAD
#  Aggiungi questo blocco al tuo main.py, DOPO la route /api/interpreta.
#  Usa la stessa DEEPSEEK_API_KEY già presente nell'Environment di Render.
#  Non tocca /api/interpreta (che resta il parser dei pressati).
# ═══════════════════════════════════════════════════════════════════════════

PROMPT_NARRATORE = """Sei il NARRATORE/SUPERRISPONDITORE di un generatore di configuratori.
Ti do l'IDENTITA' di un soggetto (es. "Ambulanza neonatale"), una DESCRIZIONE e,
se presente, la lista di ASPETTI/QUOTE dichiarati nel suo disegno CAD.
Devi OSSERVARE il soggetto e proporre la DEFINIZIONE di una libreria di prodotto:
quali campi servono, quali regole di processo, quale normativa applicabile, la distinta base.
Applica conoscenza reale del dominio (per un'ambulanza: tipo A/A1, UNI EN 1789, dotazioni
sanitarie/neonatali obbligatorie, omologazione veicolo; per altri soggetti: le loro norme).

Rispondi SOLO con JSON valido, nessun testo, nessun markdown. Schema ESATTO:
{
 "nome": "nome del prodotto",
 "motore": "strati|profilo|assieme3d|dxf",
 "campi": [{"id":"slug_senza_spazi","label":"Etichetta","tipo":"testo|num|scelta","opz":["a","b"]}],
 "regoleText": ["se X allora maggiora del N%", "se Y allora avviso ..."],
 "normativa": ["riferimento normativo puntuale e pertinente al soggetto"],
 "distinta": [{"cod":"COD","nome":"Voce di distinta","um":"pz|kg|set|m"}]
}
Regole: 'motore'='dxf' se c'e' un CAD; campi concreti e specifici del soggetto (non generici);
regoleText dichiarative e leggibili; normativa REALE e pertinente (mai inventata generica se
il soggetto ha norme note); 4-12 campi. Niente campi 'opz' se tipo!='scelta'."""

@app.post("/api/ai/ask")
async def ai_ask(request: Request):
    """Narratore: dato {identita, descrizione, aspetti[]}, propone la definizione di libreria (JSON)."""
    if not DEEPSEEK_API_KEY:
        raise HTTPException(503, "DEEPSEEK_API_KEY non configurata sul server")
    body = await request.json()
    # accetta sia il formato del configuratore {system,prompt} sia {identita,descrizione,aspetti}
    identita    = (body.get("identita") or "").strip()
    descrizione = (body.get("descrizione") or body.get("prompt") or "").strip()
    aspetti     = body.get("aspetti") or []
    if not (identita or descrizione):
        raise HTTPException(400, "identita/descrizione mancanti")

    user = f"IDENTITA': {identita}\nDESCRIZIONE: {descrizione}"
    if aspetti:
        user += "\nASPETTI/QUOTE DICHIARATI NEL CAD:\n- " + "\n- ".join(str(a) for a in aspetti)

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": PROMPT_NARRATORE},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"DeepSeek HTTP {e.code}: {e.read().decode('utf-8')[:200]}")
    except Exception as e:
        raise HTTPException(502, f"errore DeepSeek: {e}")

    try:
        contenuto = data["choices"][0]["message"]["content"]
        contenuto = contenuto.replace("```json", "").replace("```", "").strip()
        definizione = json.loads(contenuto)
    except Exception as e:
        raise HTTPException(502, f"risposta DeepSeek non interpretabile: {e}")

    # il configuratore legge d.text come JSON: lo restituisco sia grezzo sia annidato in 'text'
    return {"ok": True, "text": json.dumps(definizione, ensure_ascii=False), **definizione}

# ══════════════════ FRONT-END STATICO ══════════════════
@app.get("/", response_class=HTMLResponse)
def home():
    if os.path.exists(HTML_FILE):
        return FileResponse(HTML_FILE)
    return HTMLResponse("<h1>EXPAN Configuratore</h1><p>Back-end attivo. Carica il file HTML nel repo.</p>")

# ══════════════════ AVVIO (funziona anche con 'python main.py') ══════════════════
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
