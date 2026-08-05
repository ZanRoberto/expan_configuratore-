#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
COLLAUDO — Metodo Matrice / configuratore
Verifica che in un file HTML del configuratore ci siano ANCORA tutte le
correzioni fatte. Da lanciare PRIMA di ogni deploy, e ogni volta che si
riceve un file nuovo (da me, da un'altra sessione, da chiunque).

DOVE STA: nel repo, accanto a configuratore_expan_v2.html e main.py.
Va versionato col codice: e' l'elenco di cosa deve funzionare, e cresce con lui.

USO (dalla cartella del repo):
    python collaudo.py                      <- collauda configuratore_expan_v2.html
    python collaudo.py altrofile.html       <- collauda un file preciso

Se stampa anche un solo ROTTO, quel file NON va in produzione.
Ogni riga dice: che cosa deve funzionare, come si verifica, quando e'
stato corretto e che cosa succede se sparisce.
"""
import sys, re, os

# (nome, marcatore da cercare, e' una regex?, cosa succede se manca)
PROVE = [
 ("Manodopera dentro il costo",
  "costo+=costoMano;", False,
  "il pannello ANALISI mostra materiali NEGATIVI e un margine gonfiato: si vende sotto costo (03ago)"),

 ("Fresatura non pagata due volte",
  "cod:'SFRIDO'", False,
  "l'accessorio paga la sagomatura sia nel calc sia nel ciclo Fresatura (03ago)"),

 ("Colori: comandano gli strati (Three.js)",
  "_stratiColorati", False,
  "torna il TUTTO ROSSO: i campi colore ridipingono i layer gia' colorati (03ago)"),

 ("Colori: comandano gli strati (model-viewer)",
  "_stratiOK", False,
  "il ripiego model-viewer colora tutto col primo colore congelato (03ago)"),

 ("Campi colore per-strato neutralizzati, comunque si chiamino",
  r"strato\|layer\|livello", True,
  "colore_layer_N (non colore_strato_N) sfugge al filtro e riporta il tutto rosso (03ago)"),

 ("Campi colore per-strato nascosti dal form",
  "_reStrato", False,
  "quattro tendine congelate su 'rosso' contraddicono a schermo i colori veri (04ago)"),

 ("Stato MAI FATTO visibile",
  "Mai fatto", False,
  "'mai fatto' e 'riconoscimento rotto' diventano indistinguibili a schermo (03ago)"),

 ("Riconoscimento: le scelte sono le coppie della firma",
  "function _coppieDi", False,
  "l'imbuto non stringe e non arriva MAI a dire 'identico': guarda solo la misura (04ago)"),

 ("Ordine salvato con lib e cfg",
  "lib: r.lib", False,
  "l'ordine richiamato non si riapre: 'Cannot read properties of undefined' (04ago)"),

 ("Ordini vecchi: strati ricostruiti dalla firma",
  "GLI STRATI STANNO NELLA FIRMA", False,
  "l'ordine richiamato torna senza strati: lista vuota e modello di un colore solo (04ago)"),

 ("Richiamo ordine: errori distinti, non tutti 'Server non raggiungibile'",
  "tipo:'apertura'", False,
  "qualunque errore viene diagnosticato come problema di rete e non si trova mai la causa (04ago)"),

 ("Stratigrafia generata SEMPRE se il prodotto ha strati",
  "Array.isArray(cfg.strati) && cfg.strati.length>0", False,
  "con 4 strati torna il GLB (misure ignorate, onde finte), con 5 la lastra: due geometrie (05ago)"),

 ("Impilati o annidati: la stratigrafia generata solo se i pezzi sono impilati",
  "_impilati", False,
  "un cuscino sagomato (guscio+inserto+punte) viene disegnato come una pila di lastre piatte (05ago)"),

 ("Mondi non duplicati: stesso titolo aggiorna",
  "NIENTE PIU' MONDI DOPPI", False,
  "ogni salvataggio crea un mondo nuovo: quattro 'essart tende' identici nella barra (05ago)"),

 ("Eliminazione mondo disponibile",
  "function eliminaMondo", False,
  "non c'e' modo di togliere un mondo doppio una volta creato (05ago)"),

 ("Abbinamento campo-pezzo insensibile al plurale",
  "function _formeDi", False,
  "il campo COLORE POMOLI non trova il pezzo POMOLO e non colora niente (05ago)"),

 ("Controllo del nome famiglia mentre si scrive",
  "CONTROLLO DEL NOME MENTRE SI SCRIVE", False,
  "si scopre di aver creato un doppione solo alla fine, a lavoro gia' fatto (05ago)"),

 ("Lastra: onde lungo la lunghezza, facce esterne piatte, annidamento",
  "pianoSu", False,
  "onde nel verso sbagliato, spessore variabile, strati che non si annidano (04ago)"),

 ("Misure sul GLB revocate",
  "REVOCATO il parse", False,
  "il portone sezionale viene schiacciato dallo spessore pannello e SPARISCE (04ago)"),

 ("3D anche per i prodotti a strati (materasso, cuscino)",
  "function distinta3dStrati", False,
  "il materasso resta l'unico prodotto senza 3D (03ago)"),

 ("Pulsante Sezione",
  "id=\"tgSez\"", False,
  "non si torna alla sezione 2D: il pulsante era chiamato nel codice ma non esisteva (03ago)"),

 ("Pannello informazioni a richiesta (pulsante i)",
  "_INFO_APERTO", False,
  "torna il riquadro nero fisso sopra i pulsanti (04ago)"),

 ("Nessun messaggio in console all'avvio",
  "console.log", False,
  "la console si apre da sola e ruba spazio all'interfaccia (04ago)",
  True),   # <- questa prova e' INVERTITA: deve NON esserci

 ("Avvisi di rete non bloccanti",
  "_avviso(", False,
  "popup alert() che fermano il lavoro e vanno cliccati (03ago)"),

 ("Nessun alert bloccante di rete rimasto",
  "alert('Server non raggiungibile", False,
  "restano i popup bloccanti (03ago)",
  True),   # invertita
]

def main():
    # senza argomenti collauda il file del configuratore nella cartella corrente:
    # sta nel repo accanto al codice, quindi basta "python collaudo.py"
    path = sys.argv[1] if len(sys.argv) > 1 else 'configuratore_expan_v2.html'
    if not os.path.exists(path):
        print("\n  Non trovo '" + path + "' in questa cartella.")
        print("  Lancialo dalla cartella del repo, oppure: python collaudo.py <file.html>\n")
        sys.exit(2)
    s = open(path, encoding='utf-8', errors='replace').read()

    ver = re.search(r"VERSIONE_COLORI\s*=\s*'([^']*)'", s)
    print()
    print("  COLLAUDO  ·  " + path)
    print("  versione dichiarata nel file: " + (ver.group(1) if ver else "NESSUNA"))
    print("  " + "-" * 74)

    rotti = []
    for prova in PROVE:
        nome, marc, is_re, danno = prova[0], prova[1], prova[2], prova[3]
        invertita = len(prova) > 4 and prova[4]
        trovato = bool(re.search(marc, s)) if is_re else (marc in s)
        ok = (not trovato) if invertita else trovato
        print(f"  {'OK   ' if ok else 'ROTTO'}  {nome}")
        if not ok:
            rotti.append((nome, danno))

    print("  " + "-" * 74)
    if not rotti:
        print(f"  TUTTO A POSTO — {len(PROVE)} prove superate. Il file puo' andare in produzione.")
    else:
        print(f"  {len(rotti)} PROVE FALLITE. NON mandare questo file in produzione.")
        for nome, danno in rotti:
            print(f"\n   x {nome}")
            print(f"     se manca: {danno}")
    print()
    sys.exit(0 if not rotti else 1)

if __name__ == '__main__':
    main()
