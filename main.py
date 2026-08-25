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
from pydantic import BaseModel
import os, sqlite3, datetime, json, base64, hmac
import urllib.request, urllib.error
from fastapi import FastAPI, HTTPException, Header, Request, Body, Response
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

DB_PATH   = os.environ.get("DB_PATH", "./data/expan.db")   # cartella locale del progetto, sempre scrivibile
API_KEY   = os.environ.get("EXPORT_API_KEY", "cambia-questa-chiave")
HTML_FILE = os.environ.get("HTML_FILE", "configuratore_expan_v2.html")

# ══════════════════ VERSIONE E DIAGNOSI DEL DISCO ══════════════════
# (21ago2026) Ogni cliente ha la SUA istanza con la SUA copia del programma.
# Senza un numero di versione dichiarato non c'e' modo di sapere chi e' rimasto
# indietro dopo un aggiornamento del motore: si scopre dai difetti, troppo tardi.
VERSIONE = "2026.08.21"

def diagnosi_disco():
    """Dice la verita' su DOVE stiamo scrivendo.

    Il pericolo non e' il disco che manca: e' il disco che manca IN SILENZIO.
    get_con() crea la cartella se non c'e', SQLite ci scrive, tutto sembra
    funzionare — e al riavvio del container i dati non ci sono piu', perche'
    quella cartella non era un disco persistente ma spazio temporaneo.
    Qui si controlla e si dichiara, cosi' il guasto si vede PRIMA di perdere
    qualcosa e non dopo."""
    d = os.path.dirname(os.path.abspath(DB_PATH)) or "."
    info = {"db_path": os.path.abspath(DB_PATH), "cartella": d}
    try:
        info["cartella_esiste"]  = os.path.isdir(d)
        info["cartella_scrivibile"] = os.access(d, os.W_OK) if os.path.isdir(d) else False
        # un disco Render e' un punto di mount separato dalla radice
        info["disco_montato"]    = os.path.ismount(d)
        info["file_esiste"]      = os.path.exists(DB_PATH)
        info["file_byte"]        = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    except Exception as e:
        info["errore"] = str(e)
    # il verdetto in chiaro: chi legge non deve interpretare
    if not info.get("cartella_esiste"):
        info["persistenza"] = "NO — la cartella non esiste"
    elif not info.get("cartella_scrivibile"):
        info["persistenza"] = "NO — cartella non scrivibile"
    elif not info.get("disco_montato"):
        info["persistenza"] = ("ATTENZIONE — nessun disco montato su questa cartella: "
                               "i dati si perdono al riavvio del container")
    else:
        info["persistenza"] = "OK — disco persistente montato"
    return info

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title="EXPAN Configuratore", version="1.0", lifespan=lifespan)

# ══════════════════ PORTA A CHIAVE (Basic Auth su TUTTO il sito) ══════════════════
# Protezione temporanea, solo per te: nessuno vede nemmeno l'HTML senza credenziali.
# Si ATTIVA solo se imposti SITE_PASSWORD su Render (Environment). Senza, il sito resta
# com'era (così un deploy non ti chiude fuori). Utente/password NON sono nel codice: env.
SITE_USER     = os.environ.get("SITE_USER", "roberto")
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")   # ← imposta questa su Render per accendere la protezione

@app.middleware("http")
async def porta_a_chiave(request: Request, call_next):
    if not SITE_PASSWORD:                      # protezione spenta finché non imposti la password
        return await call_next(request)
    if request.url.path.startswith("/api/"):   # le chiamate dati restano libere: la password protegge la PAGINA (il codice sorgente, servito su "/"), non gli endpoint di salvataggio. Isolamento vero dei dati → arriverà col login per-partner.
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    ok = False
    if auth.startswith("Basic "):
        try:
            u, _, p = base64.b64decode(auth[6:]).decode("utf-8", "ignore").partition(":")
            ok = hmac.compare_digest(u, SITE_USER) and hmac.compare_digest(p, SITE_PASSWORD)
        except Exception:
            ok = False
    if not ok:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="EXPAN"'})
    return await call_next(request)

# ══════════════════ DB ══════════════════
def get_con():
    d = os.path.dirname(DB_PATH)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            # (21ago2026) PRIMA QUI C'ERA except: pass — l'errore spariva e SQLite
            # scriveva dove capitava. E' il modo in cui si perdono i dati senza
            # accorgersene. Ora si sente.
            print("[DB] ATTENZIONE: non riesco a creare la cartella %s: %s" % (d, e), flush=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def init_db():
    con = get_con()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS contatori(
        chiave TEXT PRIMARY KEY, anno INTEGER NOT NULL, valore INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS anagrafica(
      azienda_id TEXT NOT NULL DEFAULT 'default',
      tipo       TEXT NOT NULL,            -- fornitori | costi | listini | ...
      dati_json  TEXT NOT NULL,
      aggiornato TEXT NOT NULL,
      PRIMARY KEY(azienda_id, tipo)
    );
    CREATE TABLE IF NOT EXISTS offerte(
        numero TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        cliente_cod TEXT,
        stato TEXT NOT NULL DEFAULT 'bozza',   -- bozza | confermata | passata
        creata_da TEXT,
        payload_json TEXT);
    -- INDICE FIRME: una riga per ogni prodotto-firma di ogni ordine.
    -- Separata dal JSON e indicizzata -> ricerca 'gia' fatto' istantanea a
    -- qualsiasi dimensione (anni di ordini). Si prevengono le lentezze future.
    CREATE TABLE IF NOT EXISTS firme(
        firma        TEXT NOT NULL,          -- hash corto della configurazione
        numero       TEXT NOT NULL,          -- l'ordine che la contiene
        cliente_cod  TEXT,
        data         TEXT,
        descrizione  TEXT,
        prezzo       REAL,
        firma_testo  TEXT,                   -- leggibile: cosa rappresenta
        scelte_json  TEXT,                   -- le scelte [campo=valore] per il matching progressivo
        lib          TEXT,                   -- il prodotto (per filtrare)
        PRIMARY KEY(firma, numero)
    );
    CREATE INDEX IF NOT EXISTS idx_firme_firma   ON firme(firma);
    CREATE INDEX IF NOT EXISTS idx_firme_cliente ON firme(cliente_cod);
    -- (24ago2026) STORICO DELLE MODIFICHE ai dati dell'azienda.
    -- Serve a due cose diverse con la stessa riga:
    --  1) chi ha cambiato cosa e quando (quando la penna passa al cliente,
    --     "il costo e' cambiato e nessuno sa perche'" non deve piu' accadere)
    --  2) la coda verso l'ERP: 'da mandare' = le righe con inviato_erp=0.
    --     Senza questa tabella non c'e' modo di sapere QUALI righe sono
    --     cambiate, perche' il blob dice solo che e' diverso, non dove.
    CREATE TABLE IF NOT EXISTS storico_dati(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        azienda_id  TEXT NOT NULL,
        tipo        TEXT NOT NULL,           -- materiali | blocchi | ...
        quando      TEXT NOT NULL,
        autore      TEXT,
        azione      TEXT NOT NULL,           -- modifica | aggiunta | rimozione
        etichetta   TEXT,                    -- come si chiama la riga toccata
        prima_json  TEXT,
        dopo_json   TEXT,
        inviato_erp INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_storico_coda
        ON storico_dati(azienda_id, inviato_erp);
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
    data_oggi = datetime.date.today().isoformat()
    con.execute(
        "INSERT INTO offerte(numero,data,cliente_cod,stato,creata_da,payload_json) VALUES(?,?,?,?,?,?);",
        (numero, data_oggi, cliente_cod, 'bozza', creata_da,
         json.dumps(payload, ensure_ascii=False) if payload is not None else None))
    # popolo l'INDICE FIRME: una riga per ogni prodotto con firma nel payload
    if payload:
        for riga in (payload.get("righe") or []):
            f = (riga.get("firma") or {})
            h = f.get("hash")
            if not h:
                continue
            testo = f.get("testo") or ""
            lib_r, _, resto = testo.partition("::")
            scelte = resto.split("|") if resto else []
            con.execute(
                "INSERT OR REPLACE INTO firme(firma,numero,cliente_cod,data,descrizione,prezzo,firma_testo,scelte_json,lib) VALUES(?,?,?,?,?,?,?,?,?);",
                (h, numero, cliente_cod, data_oggi, riga.get("descrizione"),
                 riga.get("prezzo_unitario"), testo,
                 json.dumps(scelte, ensure_ascii=False), lib_r))
    con.execute("COMMIT;")
    return numero

# ══════════════════ ENDPOINTS ══════════════════
@app.get("/api/health")
def health():
    """(21ago2026) Prima diceva solo «ok». Un «ok» che non guarda niente non
    serve a nessuno: rispondeva ok anche mentre i dati si stavano perdendo.
    Ora dice la versione, dove scrive, se il disco e' persistente, e quanto c'e'
    dentro. Si apre dal browser e in cinque secondi si sa come sta l'istanza."""
    d = diagnosi_disco()
    conteggi, ultima = {}, None
    try:
        con = get_con()
        for t in ("offerte", "firme", "anagrafica"):
            try:
                conteggi[t] = con.execute("SELECT COUNT(*) FROM %s;" % t).fetchone()[0]
            except Exception:
                conteggi[t] = None
        try:
            ultima = con.execute("SELECT MAX(data) FROM offerte;").fetchone()[0]
        except Exception:
            pass
        con.close()
    except Exception as e:
        conteggi["errore"] = str(e)
    ok = d.get("persistenza", "").startswith("OK") and conteggi.get("offerte") is not None
    return {"status": "ok" if ok else "attenzione",
            "versione": VERSIONE,
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
            "disco": d,
            "contenuto": conteggi,
            "ultima_offerta": ultima}

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

@app.get("/api/offerte/{numero}")
def leggi_offerta(numero: str):
    """Richiama un singolo ordine col suo contenuto completo (per modificarlo)."""
    con = get_con()
    row = con.execute(
        "SELECT numero,data,cliente_cod,stato,creata_da,payload_json FROM offerte WHERE numero=?;",
        (numero,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "ordine non trovato")
    payload = json.loads(row[5]) if row[5] else {}
    # 'passata' = preso in pancia dall'ERP → congelato (sola lettura)
    congelato = (row[3] == "passata")
    return {"numero": row[0], "data": row[1], "cliente_cod": row[2], "stato": row[3],
            "creata_da": row[4], "congelato": congelato, "payload": payload}

@app.get("/api/anagrafica")
def leggi_anagrafica(azienda: str = "default", tipi: str = None, indice: int = 0):
    """Le tabelle dell'azienda (fornitori, costi, listini...). Nessun dato e' nel motore:
    se l'azienda non ha ancora caricato niente, torna vuoto.

    (24ago2026) PRIMA questa funzione leggeva SEMPRE tutte le righe, sempre.
    Con 'mondi' a 8,8 MB ogni singola chiamata leggeva dal disco, deserializzava
    in memoria e rispediva quasi 9 MB anche a chi voleva tre materiali da 200 byte.
    Doppio costo: banda e CPU (json.loads + riserializzazione), a ogni richiesta.
    Ora si puo' chiedere solo cio' che serve:

      /api/anagrafica                        -> TUTTO (come prima, invariato)
      /api/anagrafica?tipi=materiali,blocchi -> solo quelle due tabelle
      /api/anagrafica?indice=1               -> l'elenco delle tabelle con peso e
                                                data, SENZA i dati dentro

    Il comportamento senza parametri e' identico a prima: il configuratore
    esistente non si accorge di niente. Chi ha bisogno di poco, chiede poco."""
    con = get_con()

    # MODO INDICE: dice quali tabelle ci sono e quanto pesano, senza aprirle.
    # Serve al menu di sinistra della console: prima doveva scaricare 8,8 MB
    # solo per sapere che esistono quattordici tabelle.
    if indice:
        rows = con.execute(
            "SELECT tipo, length(dati_json), aggiornato FROM anagrafica WHERE azienda_id=? ORDER BY tipo;",
            (azienda,)).fetchall()
        con.close()
        def gruppo(t):
            # la divisione la decide il server, non l'interfaccia: una regola che
            # vive solo nel browser non e' una regola
            if t in TABELLE_NOSTRE: return "nostre"
            if t in TABELLE_ERP:    return "erp"
            return "motore"
        return {"azienda": azienda,
                "tabelle": [{"tipo": r[0], "byte": r[1], "aggiornato": r[2],
                             "gruppo": gruppo(r[0])} for r in rows]}

    # MODO SELETTIVO: solo le tabelle chieste. I nomi non entrano mai nella query
    # come testo, solo come parametri: si costruiscono i segnaposto, non i valori.
    # ATTENZIONE alla differenza fra "tipi assente" e "tipi presente ma vuoto":
    # se il frontend costruisce male l'URL e manda ?tipi= , NON deve ricevere
    # tutto per sbaglio (sarebbero 8,8 MB non voluti). Assente = tutto (vecchio
    # comportamento). Presente = solo cio' che e' elencato, anche se e' niente.
    if tipi is None:
        rows = con.execute("SELECT tipo,dati_json FROM anagrafica WHERE azienda_id=?;",
                           (azienda,)).fetchall()
    else:
        lista = [t.strip() for t in tipi.split(",") if t.strip()]
        if not lista:
            con.close()
            return {}
        segnaposto = ",".join("?" * len(lista))
        rows = con.execute(
            "SELECT tipo,dati_json FROM anagrafica WHERE azienda_id=? AND tipo IN (%s);" % segnaposto,
            (azienda, *lista)).fetchall()
    con.close()
    return {t: json.loads(d) for (t, d) in rows}

@app.post("/api/anagrafica/{tipo}")
def scrivi_anagrafica(tipo: str, body: dict = Body(...)):
    """Sostituisce una tabella dell'azienda (import dagli Excel del cliente)."""
    azienda = body.get("azienda") or "default"
    righe = body.get("righe")
    if not isinstance(righe, list):
        raise HTTPException(400, "serve 'righe': [...]")
    con = get_con()
    con.execute("""INSERT INTO anagrafica(azienda_id,tipo,dati_json,aggiornato) VALUES(?,?,?,?)
                   ON CONFLICT(azienda_id,tipo) DO UPDATE SET dati_json=excluded.dati_json, aggiornato=excluded.aggiornato;""",
                (azienda, tipo, json.dumps(righe, ensure_ascii=False), datetime.datetime.now().isoformat(timespec="seconds")))
    con.commit(); con.close()
    return {"tipo": tipo, "azienda": azienda, "righe": len(righe)}

@app.delete("/api/anagrafica/{tipo}")
def cancella_anagrafica(tipo: str, azienda: str = "default"):
    con = get_con()
    con.execute("DELETE FROM anagrafica WHERE azienda_id=? AND tipo=?;", (azienda, tipo))
    con.commit(); con.close()
    return {"tipo": tipo, "azienda": azienda, "cancellata": True}

# ══════════════════ MANUTENZIONE DEI DATI (riga per riga) ══════════════════
# (24ago2026) Fin qui l'unico modo di scrivere era POST /api/anagrafica/{tipo},
# che SOSTITUISCE la tabella intera. Nato per l'import iniziale, fatto da una
# persona sola, una volta. Nel momento in cui la penna passa all'ufficio tecnico
# del cliente quel gesto diventa pericoloso: due persone che salvano nella stessa
# mezz'ora si cancellano il lavoro a vicenda, in silenzio, senza errore.
#
# Qui sotto la scrittura e' PER RIGA e protetta: chi salva dichiara com'era la
# tabella quando l'ha aperta ('atteso'). Se nel frattempo qualcun altro ha
# salvato, la richiesta viene RIFIUTATA con 409 invece di sovrascrivere.
# Meglio un messaggio "ricarica, e' cambiato" che una modifica sparita.
#
# NOTA ONESTA: i dati restano un blob JSON. Questo non e' la normalizzazione in
# righe vere (quella arrivera' con cod_erp e id stabili): e' il minimo che rende
# la penna consegnabile senza perdere dati. Il blob viene riscritto per intero
# a ogni salvataggio, ma solo dopo aver verificato che nessuno sia arrivato prima.

# Quali tabelle sono NOSTRE (si scrivono) e quali arrivano dall'ERP (sola lettura).
# La console mostra questa divisione; qui e' il server a farla rispettare, perche'
# una regola che vive solo nell'interfaccia non e' una regola.
TABELLE_NOSTRE = ("materiali", "blocchi", "costi", "lavorazioni",
                  "composizione", "caratteristiche", "regole", "intervista")
TABELLE_ERP    = ("clienti", "fornitori", "listini")

def risolvi_azienda(param: str = "default") -> str:
    """UNICO punto in cui si decide di quale azienda stiamo parlando.

    Oggi restituisce il parametro cosi' com'e': c'e' un cliente solo e la porta
    e' quella di sempre. Domani, col registro distributori, l'azienda si ricavera'
    da CHI E' ENTRATO e non da cosa c'e' scritto nell'indirizzo — e cambiera'
    solo questa funzione, non i venti endpoint che la chiamano.
    Finche' resta cosi', l'azienda nell'URL NON e' una misura di sicurezza."""
    return (param or "default").strip() or "default"

def _leggi_tabella(con, azienda, tipo):
    """Torna (righe, aggiornato). Tabella inesistente = lista vuota, non errore:
    una tabella mai riempita e' una tabella vuota, non un guasto."""
    row = con.execute("SELECT dati_json, aggiornato FROM anagrafica WHERE azienda_id=? AND tipo=?;",
                      (azienda, tipo)).fetchone()
    if not row:
        return [], None
    dati = json.loads(row[0])
    return (dati if isinstance(dati, list) else []), row[1]

def _etichetta(riga):
    """Come si chiama una riga, per lo storico. Non conosco lo schema delle
    tabelle del cliente, quindi uso il primo campo testuale che trovo: nelle
    anagrafiche il codice sta quasi sempre in testa."""
    if not isinstance(riga, dict):
        return str(riga)[:60]
    for v in riga.values():
        if isinstance(v, str) and v.strip():
            return v.strip()[:60]
    return "(riga senza nome)"

def _salva_tabella(con, azienda, tipo, righe, autore, azione, etichetta, prima, dopo):
    adesso = datetime.datetime.now().isoformat(timespec="seconds")
    con.execute("""INSERT INTO anagrafica(azienda_id,tipo,dati_json,aggiornato) VALUES(?,?,?,?)
                   ON CONFLICT(azienda_id,tipo) DO UPDATE SET
                     dati_json=excluded.dati_json, aggiornato=excluded.aggiornato;""",
                (azienda, tipo, json.dumps(righe, ensure_ascii=False), adesso))
    con.execute("""INSERT INTO storico_dati(azienda_id,tipo,quando,autore,azione,etichetta,prima_json,dopo_json)
                   VALUES(?,?,?,?,?,?,?,?);""",
                (azienda, tipo, adesso, autore or "sconosciuto", azione, etichetta,
                 json.dumps(prima, ensure_ascii=False) if prima is not None else None,
                 json.dumps(dopo, ensure_ascii=False) if dopo is not None else None))
    return adesso

@app.patch("/api/anagrafica/{tipo}/riga")
def modifica_riga(tipo: str, body: dict = Body(...)):
    """Cambia UNA riga. Body: {azienda, indice, riga, atteso, autore}
    'atteso' = il valore di 'aggiornato' che avevi quando hai aperto la tabella."""
    azienda = risolvi_azienda(body.get("azienda"))
    if tipo not in TABELLE_NOSTRE:
        raise HTTPException(403, "questa tabella arriva dall'ERP: si legge, non si scrive")
    indice = body.get("indice")
    nuova  = body.get("riga")
    if not isinstance(indice, int) or not isinstance(nuova, dict):
        raise HTTPException(400, "servono 'indice' (numero) e 'riga' (oggetto)")
    con = get_con()
    try:
        con.execute("BEGIN IMMEDIATE;")
        righe, aggiornato = _leggi_tabella(con, azienda, tipo)
        atteso = body.get("atteso")
        if atteso and aggiornato and atteso != aggiornato:
            con.execute("ROLLBACK;")
            raise HTTPException(409, "qualcun altro ha salvato mentre lavoravi: ricarica la tabella")
        if indice < 0 or indice >= len(righe):
            con.execute("ROLLBACK;")
            raise HTTPException(404, "riga non trovata")
        prima = righe[indice]
        righe[indice] = nuova
        adesso = _salva_tabella(con, azienda, tipo, righe, body.get("autore"),
                                "modifica", _etichetta(nuova), prima, nuova)
        con.execute("COMMIT;")
    except HTTPException:
        con.close(); raise
    except Exception as e:
        try: con.execute("ROLLBACK;")
        except Exception: pass
        con.close(); raise HTTPException(500, "non sono riuscito a salvare: %s" % e)
    con.close()
    return {"tipo": tipo, "indice": indice, "aggiornato": adesso, "righe": len(righe)}

@app.post("/api/anagrafica/{tipo}/riga")
def aggiungi_riga(tipo: str, body: dict = Body(...)):
    """Aggiunge una riga in fondo. Body: {azienda, riga, atteso, autore}"""
    azienda = risolvi_azienda(body.get("azienda"))
    if tipo not in TABELLE_NOSTRE:
        raise HTTPException(403, "questa tabella arriva dall'ERP: si legge, non si scrive")
    nuova = body.get("riga")
    if not isinstance(nuova, dict):
        raise HTTPException(400, "serve 'riga' (oggetto)")
    con = get_con()
    try:
        con.execute("BEGIN IMMEDIATE;")
        righe, aggiornato = _leggi_tabella(con, azienda, tipo)
        atteso = body.get("atteso")
        if atteso and aggiornato and atteso != aggiornato:
            con.execute("ROLLBACK;")
            raise HTTPException(409, "qualcun altro ha salvato mentre lavoravi: ricarica la tabella")
        righe.append(nuova)
        adesso = _salva_tabella(con, azienda, tipo, righe, body.get("autore"),
                                "aggiunta", _etichetta(nuova), None, nuova)
        con.execute("COMMIT;")
    except HTTPException:
        con.close(); raise
    except Exception as e:
        try: con.execute("ROLLBACK;")
        except Exception: pass
        con.close(); raise HTTPException(500, "non sono riuscito a salvare: %s" % e)
    con.close()
    return {"tipo": tipo, "indice": len(righe) - 1, "aggiornato": adesso, "righe": len(righe)}

@app.delete("/api/anagrafica/{tipo}/riga")
def rimuovi_riga(tipo: str, indice: int, azienda: str = "default",
                 atteso: str = None, autore: str = None):
    """Toglie UNA riga. Lo storico conserva com'era: si puo' sempre dire cosa c'era."""
    azienda = risolvi_azienda(azienda)
    if tipo not in TABELLE_NOSTRE:
        raise HTTPException(403, "questa tabella arriva dall'ERP: si legge, non si scrive")
    con = get_con()
    try:
        con.execute("BEGIN IMMEDIATE;")
        righe, aggiornato = _leggi_tabella(con, azienda, tipo)
        if atteso and aggiornato and atteso != aggiornato:
            con.execute("ROLLBACK;")
            raise HTTPException(409, "qualcun altro ha salvato mentre lavoravi: ricarica la tabella")
        if indice < 0 or indice >= len(righe):
            con.execute("ROLLBACK;")
            raise HTTPException(404, "riga non trovata")
        prima = righe.pop(indice)
        adesso = _salva_tabella(con, azienda, tipo, righe, autore,
                                "rimozione", _etichetta(prima), prima, None)
        con.execute("COMMIT;")
    except HTTPException:
        con.close(); raise
    except Exception as e:
        try: con.execute("ROLLBACK;")
        except Exception: pass
        con.close(); raise HTTPException(500, "non sono riuscito a salvare: %s" % e)
    con.close()
    return {"tipo": tipo, "rimossa": _etichetta(prima), "aggiornato": adesso, "righe": len(righe)}

@app.get("/api/anagrafica/storico")
def storico_dati(azienda: str = "default", tipo: str = None, quante: int = 50):
    """Chi ha cambiato cosa, dal piu' recente."""
    azienda = risolvi_azienda(azienda)
    con = get_con()
    q = "SELECT quando,autore,tipo,azione,etichetta,inviato_erp FROM storico_dati WHERE azienda_id=?"
    args = [azienda]
    if tipo:
        q += " AND tipo=?"; args.append(tipo)
    q += " ORDER BY id DESC LIMIT ?;"; args.append(max(1, min(quante, 500)))
    rows = con.execute(q, args).fetchall(); con.close()
    return [{"quando": r[0], "autore": r[1], "tipo": r[2], "azione": r[3],
             "riga": r[4], "inviato_erp": bool(r[5])} for r in rows]

# ══════════════════ CODA VERSO L'ERP ══════════════════
@app.get("/api/erp/da_inviare")
def erp_da_inviare(azienda: str = "default"):
    """Quante modifiche aspettano di uscire, e quando e' stato l'ultimo invio.
    E' il conteggio che la console mostra in fondo alla pagina."""
    azienda = risolvi_azienda(azienda)
    con = get_con()
    n = con.execute("SELECT COUNT(*) FROM storico_dati WHERE azienda_id=? AND inviato_erp=0;",
                    (azienda,)).fetchone()[0]
    ultimo = con.execute("SELECT MAX(quando) FROM storico_dati WHERE azienda_id=? AND inviato_erp=1;",
                         (azienda,)).fetchone()[0]
    righe = con.execute("""SELECT quando,autore,tipo,azione,etichetta FROM storico_dati
                           WHERE azienda_id=? AND inviato_erp=0 ORDER BY id;""", (azienda,)).fetchall()
    con.close()
    return {"da_inviare": n, "ultimo_invio": ultimo,
            "modifiche": [{"quando": r[0], "autore": r[1], "tipo": r[2],
                           "azione": r[3], "riga": r[4]} for r in righe]}

@app.post("/api/erp/prepara")
def erp_prepara(body: dict = Body(None)):
    """Prepara il pacchetto per l'ERP. Body: {azienda, conferma}

    Senza 'conferma' e' un'ANTEPRIMA: mostra cosa uscirebbe e non tocca niente.
    Con 'conferma': true segna le modifiche come inviate.
    Escono solo le tabelle nostre — costi d'acquisto e codici dell'ERP non
    tornano indietro da qui: il flusso e' a senso unico, per costruzione."""
    body = body or {}
    azienda = risolvi_azienda(body.get("azienda"))
    con = get_con()
    righe = con.execute("""SELECT id,quando,autore,tipo,azione,etichetta,dopo_json
                           FROM storico_dati WHERE azienda_id=? AND inviato_erp=0 ORDER BY id;""",
                        (azienda,)).fetchall()
    pacchetto = [{"quando": r[1], "autore": r[2], "tabella": r[3], "azione": r[4],
                  "riga": r[5], "dati": json.loads(r[6]) if r[6] else None}
                 for r in righe if r[3] in TABELLE_NOSTRE]
    conferma = bool(body.get("conferma"))
    if conferma and righe:
        con.execute("UPDATE storico_dati SET inviato_erp=1 WHERE azienda_id=? AND inviato_erp=0;",
                    (azienda,))
        con.commit()
    con.close()
    return {"schema": "MATRICE-ANAGRAFICA-ERP", "versione": "1.0",
            "azienda": azienda, "anteprima": not conferma,
            "generato": datetime.datetime.now().isoformat(timespec="seconds"),
            "conteggio": len(pacchetto), "modifiche": pacchetto}

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

@app.get("/api/gia_fatto")
def gia_fatto(firma: str = "", cliente: str = ""):
    """RICONOSCIMENTO: data una firma (hash), dice se quel prodotto è già stato
    fatto, e per chi. È il motore del 'già fatto / per questo cliente / per altri'.
    Cerca dentro i payload salvati la firma esatta (hash indicizzabile)."""
    if not firma:
        raise HTTPException(400, "manca il parametro firma")
    con = get_con()
    # RICERCA INDICIZZATA: salta diretta alle righe con questa firma (idx_firme_firma).
    # Costa uguale con 18 ordini o con 15 milioni: millisecondi.
    rows = con.execute(
        "SELECT numero, data, cliente_cod, descrizione, prezzo FROM firme WHERE firma=? ORDER BY data DESC",
        (firma,)
    ).fetchall()
    con.close()
    per_cliente, per_altri = [], []
    for numero, data, cli, descr, prezzo in rows:
        voce = {"numero": numero, "data": data, "cliente": cli,
                "descrizione": descr, "prezzo": prezzo}
        if cliente and cli == cliente:
            per_cliente.append(voce)
        else:
            per_altri.append(voce)
    return {
        "firma": firma,
        "gia_fatto": bool(rows),
        "per_questo_cliente": per_cliente,
        "per_altri_clienti": per_altri,
        "quante_volte": len(rows)
    }


@app.post("/api/reindex_firme")
def reindex_firme():
    """Ricostruisce l'indice firme dai payload gia' salvati. Utile una volta,
    dopo aver introdotto l'indice, per gli ordini caricati prima."""
    con = get_con()
    con.execute("DELETE FROM firme;")
    rows = con.execute("SELECT numero, data, cliente_cod, payload_json FROM offerte").fetchall()
    n = 0
    for numero, data, cli, pj in rows:
        if not pj:
            continue
        try:
            pay = json.loads(pj)
        except Exception:
            continue
        for riga in (pay.get("righe") or []):
            f = (riga.get("firma") or {})
            h = f.get("hash")
            if not h:
                continue
            testo = f.get("testo") or ""
            lib_r, _, resto = testo.partition("::")
            scelte = resto.split("|") if resto else []
            con.execute(
                "INSERT OR REPLACE INTO firme(firma,numero,cliente_cod,data,descrizione,prezzo,firma_testo,scelte_json,lib) VALUES(?,?,?,?,?,?,?,?,?);",
                (h, numero, cli, data, riga.get("descrizione"), riga.get("prezzo_unitario"),
                 testo, json.dumps(scelte, ensure_ascii=False), lib_r))
            n += 1
    con.commit(); con.close()
    return {"reindicizzate": n}


class RiconosciBody(BaseModel):
    lib: str = ""
    scelte: list = []
    cliente: str = ""

@app.post("/api/riconosci")
def riconosci(body: RiconosciBody):
    """RICONOSCIMENTO PROGRESSIVO: date le scelte fatte FINORA, trova gli ordini
    che le contengono TUTTE (sottoinsieme). Man mano che le scelte crescono, i
    candidati calano \u2014 l'imbuto. Distingue: esatto (stesse identiche scelte)
    da parziale (le contiene ma ha anche altro)."""
    scelte = set(body.scelte or [])
    if not scelte:
        return {"gia_fatto": False, "quanti": 0, "per_cliente": 0, "per_altri": 0, "esatto": False, "esempi": []}
    con = get_con()
    # filtro per prodotto sull'indice; poi il match sottoinsieme in Python
    rows = con.execute(
        "SELECT numero, cliente_cod, data, descrizione, prezzo, scelte_json FROM firme WHERE lib=?",
        (body.lib,)
    ).fetchall()
    con.close()
    per_cliente = 0
    per_altri = 0
    esatti = 0
    esempi_cli = []
    esempi_altri = []
    for numero, cli, data, descr, prezzo, sj in rows:
        try:
            salvate = set(json.loads(sj) if sj else [])
        except Exception:
            continue
        if scelte.issubset(salvate):          # l'ordine contiene tutte le scelte fatte finora
            e_esatto = (scelte == salvate)
            if e_esatto:
                esatti += 1
            voce = {"numero": numero, "cliente": cli, "data": data,
                    "descrizione": descr, "prezzo": prezzo, "esatto": e_esatto}
            if body.cliente and cli == body.cliente:
                per_cliente += 1
                if len(esempi_cli) < 3: esempi_cli.append(voce)
            else:
                per_altri += 1
                if len(esempi_altri) < 3: esempi_altri.append(voce)
    return {
        "gia_fatto": (per_cliente + per_altri) > 0,
        "quanti": per_cliente + per_altri,
        "per_cliente": per_cliente,
        "per_altri": per_altri,
        "esatto": esatti > 0,
        "esempi_cliente": esempi_cli,
        "esempi_altri": esempi_altri
    }


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
 "motore": "strati|serie|forma|dxf|glb",
 "campi": [{"id":"slug","ruolo":"altezza|passo|numero|rotazione|raggio_interno|raggio_esterno|lunghezza|larghezza|diametro|spessore|onde|colore|materiale|finitura|corrimano|null","label":"Etichetta","tipo":"testo|num|scelta","opz":["a","b"],"std":["1800","2000"],"min":1600,"max":2400,"cod":{"a":"AAA"}}],
 "regoleText": ["se X allora maggiora del N%", "se Y allora avviso ..."],
 "normativa": ["riferimento normativo puntuale e pertinente al soggetto"],
 "distinta": [{"cod":"COD-${VAR(campo)}","nome":"Voce ${campo}","um":"pz|kg|set|m"}]
}
IL RUOLO — LA COSA PIU' IMPORTANTE DI TUTTE.
Ogni campo che serve al DISEGNO deve dichiarare il suo "ruolo". Il motore legge il RUOLO,
non il nome: puoi chiamare un campo "alzata", "passo", "altezza_pannello" o "interasse" —
se il ruolo e' "passo", il motore lo capisce. Senza ruolo, il prodotto esce sbagliato.

I ruoli:
  "altezza"        l'altezza/lunghezza totale del prodotto
  "passo"          il dislivello o l'interasse fra un pezzo e il successivo (alzata, altezza pannello)
  "numero"         il numero di pezzi, se lo si dichiara invece di calcolarlo
  "rotazione"      i gradi TOTALI del giro (0 = pila diritta)
  "raggio_interno" il palo/mozzo centrale (0 se non c'e')
  "raggio_esterno" il raggio esterno del pezzo · o il raggio degli angoli in "forma"
  "lunghezza" "larghezza" "diametro" "spessore"     le misure del pezzo
  "onde"           il numero di onde interne
  "colore" "materiale" "finitura" "corrimano"       le scelte
Un campo che non serve al disegno (es. "tipo_scorrimento", "imballo") NON mette il ruolo.

LA CASSETTA DEI DISEGNATORI — scegli "motore" guardando COM'E' FATTO il soggetto.
Questa e' la decisione piu' importante: il configuratore disegna solo cio' che sa fare.

"strati" — il prodotto e' FATTO DI LASTRE SOVRAPPOSTE e incollate.
    (materasso, pressato, pannello sandwich, cartone ondulato liner+onda+liner)
    campi tipici: lunghezza, larghezza, spessore_totale.
    Se il profilo fra una lastra e l'altra e' ondulato aggiungi il campo "onde"
    (numero di onde, std ["5","3","7"]) — le onde sono INTERNE: sopra e sotto e' piatto.

"serie" — il prodotto e' N PEZZI UGUALI RIPETUTI CON UNA LEGGE.
    (scala a chiocciola: ogni gradino sale e ruota · portone sezionale: ogni pannello sale ·
     doghe, gradonate, ringhiere: il pezzo si ripete)
    campi OBBLIGATORI, con questi id esatti:
      altezza (totale, mm) · alzata (passo fra un pezzo e il successivo, mm) ·
      rotazione (gradi TOTALI del giro: 360 = un giro intero, 0 = pila diritta senza rotazione) ·
      raggio_interno (palo centrale, 0 se non c'e') · raggio_esterno · spessore (pedata/pannello)
    il numero dei pezzi NON si dichiara: nasce da altezza / alzata.

"forma" — il prodotto e' UN PROFILO PIANO TIRATO IN SPESSORE.
    (scatola, fustella, pannello sagomato, piastra, guarnizione, coperchio)
    campi OBBLIGATORI, con questi id esatti:
      forma (scelta: "Rettangolo"|"Poligono N lati"|"Ellisse") · lunghezza · larghezza ·
      raggio (arrotondamento degli angoli, 0 = spigoli vivi) · lati (se poligono) · diametro (se poligono)

"dxf" — SOLO se il cliente ha allegato un disegno CAD.
"glb" — SOLO se il cliente ha allegato un modello 3D.

Se il soggetto non entra in nessuna famiglia, scegli la piu' vicina e scrivi una regola
che dichiara cosa il disegno NON rappresenta. Non inventare motori che non esistono.

Regole: 'motore'='dxf' se c'e' un CAD; campi concreti e specifici del soggetto (non generici);
regoleText dichiarative e leggibili; normativa REALE e pertinente (mai inventata generica se
il soggetto ha norme note); 4-12 campi. Niente campi 'opz' se tipo!='scelta'.

MISURE — OBBLIGATORIO: ogni campo "tipo":"num" DEVE avere "std": le misure STANDARD reali
del settore per quel campo (3-6 valori, dal piu' comune in poi, solo numeri come stringhe).
Il PRIMO valore di "std" e' quello con cui il prodotto nasce: dev'essere una misura VALIDA
e conforme alle regole che dichiari (mai 0, mai un valore che le tue stesse regole vietano).
Esempi: lunghezza materasso std ["190","200","210"] min 180 max 220; larghezza ["80","90","120","160","180"] min 60 max 200;
lunghezza vano ambulanza std ["2500","2800","3000","3300"] min 2200 max 3600.

CONTROLLO — OBBLIGATORIO: ogni campo "tipo":"num" DEVE avere anche "min" e "max": i limiti
REALI entro cui una misura fuori standard e' ancora producibile. Servono a impedire che
l'operatore inserisca misure impossibili (es. una lunghezza di 999 su un materasso).
"min" e "max" sono numeri, coerenti col settore e con gli std dichiarati.
DISTINTA — LE VARIANTI NEL CODICE: una voce che CAMBIA con una scelta del cliente deve
portarsela nel codice e nella descrizione, cosi' il gestionale riceve l'articolo giusto.
Si scrive col nome del campo fra ${...}:
  {"cod":"GRAD-001-${VAR(colore)}", "nome":"Gradino ${materiale_pedata} ${colore}", "um":"pz"}
  → con colore=rosso e materiale_pedata=rovere diventa:  GRAD-001-ROSSO · "Gradino rovere rosso"
VAR(campo) = la SIGLA del valore: serve per il CODICE.

LE SIGLE — il gestionale vuole codici suoi, non i nomi per esteso: GRA#ROS23ROV, non
GRA#ROSSO23ROVERE. Quindi ogni valore di una scelta che entra nel codice deve avere la
sua SIGLA, dichiarata cosi' nel campo:
  {"id":"colore","label":"Colore","tipo":"scelta","opz":["rosso","nero","bianco"],
   "cod":{"rosso":"ROS","nero":"NER","bianco":"BIA"}}
  {"id":"materiale","tipo":"scelta","opz":["rovere","faggio"],"cod":{"rovere":"ROV","faggio":"FAG"}}
Le sigle sono di 2-3 lettere, maiuscole, come si usa nei gestionali.
Mettile SOLO sui campi che entrano nel codice articolo (colore, materiale, finitura, misura
codificata): non su tutti.
${campo} da solo = il valore com'e': serve per la DESCRIZIONE.
Mettile SOLO sulle voci che cambiano davvero: il gradino cambia col colore, la bulloneria no.

Le regole dichiarative usano il formato: "se CAMPO = 'VALORE' allora CAMPO2 deve essere 'VALORE2'"
(cosi' il configuratore le fa rispettare da solo)."""

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
DATI_FILE = os.environ.get("DATI_FILE", "dati_azienda.html")

@app.get("/dati", response_class=HTMLResponse)
def pagina_dati():
    """La console dei dati dell'azienda (livello zero del Matrice).
    Vive accanto al configuratore, non dentro: due pagine, una sola API, un solo
    database. Se un giorno questa va riscritta da zero, il configuratore non se
    ne accorge — e viceversa."""
    if os.path.exists(DATI_FILE):
        return FileResponse(DATI_FILE)
    return HTMLResponse(
        "<h1>Dati azienda</h1><p>Manca il file <code>%s</code> nel progetto.</p>" % DATI_FILE,
        status_code=404)

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
