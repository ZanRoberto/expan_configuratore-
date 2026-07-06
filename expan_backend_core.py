#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EXPAN back-end — CUORE: numerazione atomica delle offerte su SQLite.
Questo è il pezzo che l'HTML non poteva fare: garantire che due terminali
che chiedono un numero nello stesso istante ottengano numeri DIVERSI e progressivi.

Usa solo la standard library (sqlite3) → eseguibile e testabile qui.
In produzione questa logica sta dentro un endpoint FastAPI, ma la garanzia
di atomicità è QUESTA, indipendente dal web framework.
"""
import sqlite3, os, datetime, threading

DB_PATH = "/home/claude/expan.db"

# ══════════════════ SCHEMA ══════════════════
def init_db(path=DB_PATH, reset=False):
    if reset and os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL;")   # migliora concorrenza lettura/scrittura
    con.executescript("""
    CREATE TABLE IF NOT EXISTS contatori (
        chiave   TEXT PRIMARY KEY,
        anno     INTEGER NOT NULL,
        valore   INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS offerte (
        numero        TEXT PRIMARY KEY,
        data          TEXT NOT NULL,
        cliente_cod   TEXT,
        stato         TEXT NOT NULL DEFAULT 'bozza',
        creata_da     TEXT,
        payload_json  TEXT
    );
    """)
    con.commit()
    con.close()

# ══════════════════ NUMERAZIONE ATOMICA ══════════════════
def prossimo_numero(cliente_cod=None, creata_da="sistema", path=DB_PATH):
    """
    Assegna un numero offerta univoco e progressivo per anno, in modo ATOMICO.
    La sequenza è: BEGIN IMMEDIATE (lock scrittura) → incrementa contatore →
    inserisci offerta → COMMIT. Due chiamate simultanee non possono ottenere
    lo stesso numero perché BEGIN IMMEDIATE serializza le scritture.
    """
    anno = datetime.datetime.now().year
    con = sqlite3.connect(path, timeout=30)  # attende invece di fallire su lock
    try:
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE;")       # acquisisce subito il lock di scrittura
        row = con.execute(
            "SELECT valore, anno FROM contatori WHERE chiave='offerta';"
        ).fetchone()
        if row is None:
            nuovo = 1
            con.execute("INSERT INTO contatori(chiave,anno,valore) VALUES('offerta',?,?);", (anno, nuovo))
        else:
            valore, anno_salvato = row
            # reset del progressivo al cambio d'anno
            nuovo = 1 if anno_salvato != anno else valore + 1
            con.execute("UPDATE contatori SET valore=?, anno=? WHERE chiave='offerta';", (nuovo, anno))
        numero = f"OFF-{anno}-{nuovo:04d}"
        con.execute(
            "INSERT INTO offerte(numero,data,cliente_cod,stato,creata_da) VALUES(?,?,?,?,?);",
            (numero, datetime.date.today().isoformat(), cliente_cod, 'bozza', creata_da)
        )
        con.execute("COMMIT;")
        return numero
    except Exception:
        con.execute("ROLLBACK;")
        raise
    finally:
        con.close()

# ══════════════════ TEST CONCORRENZA REALE ══════════════════
if __name__ == "__main__":
    print("=== init database ===")
    init_db(reset=True)

    # simulo N terminali che chiedono un numero NELLO STESSO ISTANTE
    N = 50
    risultati = []
    lock = threading.Lock()
    barriera = threading.Barrier(N)   # tutti i thread partono insieme → massima collisione

    def terminale(tid):
        barriera.wait()               # sincronizza la partenza: tutti insieme
        num = prossimo_numero(cliente_cod=f"CLI{tid%3}", creata_da=f"terminale-{tid}")
        with lock:
            risultati.append(num)

    threads = [threading.Thread(target=terminale, args=(i,)) for i in range(N)]
    print(f"=== {N} terminali chiedono un numero SIMULTANEAMENTE ===")
    for t in threads: t.start()
    for t in threads: t.join()

    # VERIFICA: tutti diversi, progressivi, nessun buco
    unici = set(risultati)
    print(f"numeri generati:      {len(risultati)}")
    print(f"numeri UNICI:         {len(unici)}")
    print(f"collisioni:           {len(risultati)-len(unici)}")
    numeri_ord = sorted(int(n.split('-')[-1]) for n in risultati)
    atteso = list(range(1, N+1))
    print(f"sequenza completa 1..{N} senza buchi: {numeri_ord == atteso}")
    print(f"esempio: {sorted(risultati)[:3]} ... {sorted(risultati)[-2:]}")

    # verifica persistenza: le offerte sono nel DB
    con = sqlite3.connect(DB_PATH)
    n_db = con.execute("SELECT COUNT(*) FROM offerte;").fetchone()[0]
    contatore = con.execute("SELECT valore FROM contatori WHERE chiave='offerta';").fetchone()[0]
    con.close()
    print(f"offerte persistite nel DB: {n_db} | contatore finale: {contatore}")

    ok = (len(unici)==N and numeri_ord==atteso and n_db==N)
    print("\n" + ("✓ NUMERAZIONE ATOMICA VERIFICATA — nessuna collisione sotto concorrenza reale"
                  if ok else "✗ FALLITO"))
