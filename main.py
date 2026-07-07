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
