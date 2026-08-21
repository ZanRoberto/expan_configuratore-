# TRACCIATO PER L'ERP — ordini, fabbisogno, magazzino
**Metodo Matrice · 19 agosto 2026 · Roberto Zannoni (Tecnaria)**

Documento tecnico per chi integra l'ERP. Aggiorna e completa il documento NTS già prodotto
(modello PULL, payload, anagrafica clienti): **se quel file è disponibile, va allineato a questo.**

---

## 0. IL PRINCIPIO, IN UNA RIGA

> **Matrice propone. L'ERP dispone.**

Matrice **non scrive mai un movimento di magazzino.** Genera il *fabbisogno* di una commessa —
cosa serve, quanto, con quale resa teorica — e lo mette a disposizione. L'ERP decide se e come
registrarlo, con le sue causali, le sue unità di misura, le sue regole.

Il motivo non è tecnico ma di responsabilità: **delle rimanenze risponde l'azienda davanti al
fisco.** Chi ne risponde, le tiene. E c'è un motivo pratico altrettanto forte: Matrice conosce
il consumo **teorico**; il consumo **reale** (rese fuori tolleranza, sfridi recuperati, lastre
rovinate) lo vede solo il laboratorio. Un magazzino scaricato col teorico diverge dal reale in
poche settimane, e nessuno saprà più quale dei due numeri era sbagliato.

---

## 1. COME L'ERP PRENDE I DATI (modalità PULL — già attiva)

```
GET https://<istanza-cliente>.onrender.com/api/export?stato=confermata
Header: X-API-Key: <chiave concordata>
```

Risponde con le offerte nello stato richiesto. Nessuna push, nessun webhook: **è l'ERP che
viene a prendere**, quando vuole lui. Nessuno entra nell'ERP e l'ERP non deve aprire porte.

Stati disponibili: `bozza` (preventivo) · `confermata` (ordine accettato) · `passata` (già presa dall'ERP).

---

## 2. COSA C'È GIÀ NEL JSON (verificato sul codice, non promesso)

```jsonc
{
  "schema": "EXPAN-EXPORT-ERP", "versione": "1.0",
  "generato": "2026-08-19T12:24:00",
  "conteggio": 3,
  "offerte": [{
    "numero": "2026/0184", "data": "2026-08-19",
    "cliente_cod": "MATVEN", "stato": "confermata",
    "offerta": {
      "schema": "MM-1", "mondo": "materassi", "marchio": "DT GROUP",
      "cliente": "MATVEN", "agente": "", "data": "2026-08-19",
      "righe": [{
        "articolo_padre":  "MAT-BABILONIA",
        "codice_variante": "MAT-BABILONIA#160,200,MEM",
        "grammatica": { "padre":"MAT-BABILONIA", "segmenti":["misura","strati"],
                        "lunghezze":{}, "riempimento":"zeri",
                        "sep_padre":"#", "sep_varianti":"," },
        "descrizione": "Materasso Babilonia 160x200 · 8 strati",
        "quantita": 2,
        "varianti": [
          { "campo":"misura", "etichetta":"Misura", "valore":"160x200",
            "descrizione":"Misura: 160x200", "sigla":"160200" }
        ],
        "distinta": [ { "cod":"MEM35", "descrizione":"Memory HR35 3cm", "um":"m2", "qta":0.96 } ],
        "cicli":    [ { "reparto":"Taglio", "fase":"taglio lastre", "minuti":14 } ],
        "prezzo_unitario": 201.79, "costo_unitario": 146.22,
        "prezzo_origine": "listino", "ricarico_applicato": 1.38
      }]
    }
  }]
}
```

**Nota importante sulle quantità:** `distinta[].qta` è **per unità di prodotto**.
Il fabbisogno della riga è `qta × quantita`. Va scritto nel contratto d'integrazione, perché è
il classico punto in cui si sbaglia di un fattore 2 e nessuno se ne accorge per mesi.

---

## 3. COSA MANCA E VA AGGIUNTO (il pezzo magazzino)

### 3.1 Blocco `fabbisogno` per riga — il cuore

Oggi la `distinta` dice *cosa serve*. All'ERP serve anche *come si aggancia al suo magazzino*
e *quanto è affidabile il numero*:

```jsonc
"fabbisogno": [{
  "cod_matrice":   "MEM35",          // il nostro codice
  "cod_erp":       "MP-0451",        // ⚠ OGGI NON ESISTE — vedi 3.2
  "descrizione":   "Memory HR35 lastra 3 cm",
  "um":            "m2",
  "qta_unitaria":  0.96,             // per un pezzo
  "qta_totale":    1.92,             // × quantita riga: il numero da usare
  "resa_teorica":  0.92,             // resa dichiarata in anagrafica
  "scarto_previsto_pct": 8,
  "natura":        "materia_prima",  // materia_prima | semilavorato | accessorio | imballo
  "origine_dato":  "calcolata"       // calcolata | dichiarata | DA_DICHIARARE
}]
```

`origine_dato` è la traduzione di una regola che il sistema applica già:
**dato mancante = `DA_DICHIARARE`, mai un numero inventato.** L'ERP deve trattare quelle righe
come da verificare, non caricarle in automatico.

### 3.2 `cod_erp` sul materiale — la chiave di aggancio

**Oggi non esiste** (zero occorrenze nel codice). È il ponte fra i due mondi: come la P.IVA per
i clienti, il codice ERP è la chiave per i materiali. Va aggiunto come colonna dell'anagrafica
MATERIALI, e riportato qui. Senza, l'ERP deve indovinare per descrizione — e sbaglia.

### 3.3 Blocco `controllo` — evita i carichi doppi

```jsonc
"controllo": {
  "id_univoco":  "MATVEN-2026/0184-v3",   // numero + versione: MAI riusato
  "versione":     3,                       // sale a ogni modifica dell'ordine
  "impronta":    "cd77ac5d",               // la firma: se non cambia, i dati non sono cambiati
  "generato_il": "2026-08-19T12:24:00"
}
```

Regola per l'ERP: **se ho già `id_univoco`, ignoro.** Senza questo, una rilettura raddoppia il
carico. È il punto che rompe più integrazioni di qualunque altro.

### 3.4 Campi di commessa

`data_consegna_richiesta`, `riferimento_cliente` (il loro numero d'ordine), `note_produzione`.
Servono all'ERP per programmare, e oggi non ci sono.

---

## 4. IL PROTOCOLLO DI PRESA (chi marca cosa)

```
1.  ERP  →  GET /api/export?stato=confermata          prende gli ordini nuovi
2.  ERP  →  registra nel suo gestionale (movimenti, commessa, magazzino)
3.  ERP  →  PATCH /api/offerte/{numero}/stato  {"stato":"passata"}
```

Il passo 3 è quello che oggi manca nell'accordo e va concordato: **finché l'ERP non dichiara di
aver preso, l'ordine resta fra i confermati e verrà riletto.** Dopo `passata` l'ordine diventa
in sola lettura anche in Matrice: non si modifica più ciò che è già in produzione.

Se l'ERP non può fare la PATCH (alcuni gestionali leggono e basta), l'alternativa è che sia
l'operatore a premere «→ passa in produzione» in Matrice dopo l'importazione. Meno pulito, ma
funziona. **Va deciso prima, non dopo.**

---

## 5. LE SEI DOMANDE DA CONCORDARE CON L'ERP (prima di scrivere codice)

1. **Unità di misura.** Noi diciamo `m2`, loro dicono `MQ`? Serve la tabella di conversione, con
   il fattore. Un materiale a metri lineari da noi e a pezzi da loro è un incidente garantito.
2. **Codice materiale.** Chi è il padrone? Proposta: l'ERP, e noi lo riportiamo in `cod_erp`.
3. **Articoli nuovi.** Quando nasce una variante, chi crea il codice a gestionale? Matrice lo
   propone (`codice_variante` + `grammatica`); l'ERP lo accetta o lo rimappa.
4. **Semilavorati.** Blocco → lastre → materasso è una **distinta a livelli**. Matrice oggi non
   la gestisce. Decidere: le lastre sono un articolo di magazzino per l'ERP, o solo una fase?
5. **Chi marca `passata`** (punto 4).
6. **Ogni quanto legge l'ERP?** Ogni ora, a fine giornata, a comando?

---

## 6. COME PARTI SENZA ASPETTARE L'ERP

Questa è la parte che ti riguarda direttamente. **Il modello PULL ha un pregio: non richiede
che l'ERP faccia niente per esistere.** Il dato viene prodotto comunque.

**Fase 1 — subito, da soli.** Matrice genera ordini, distinta, cicli, fabbisogno. Il file si
scarica **a mano** dal browser (o come CSV) e si passa all'ufficio, che lo usa come oggi usa i
suoi fogli. Nessuna integrazione, nessun permesso da chiedere. **Si parte lunedì.**

**Fase 2 — quando l'ERP c'è ma non si muove.** Un'esportazione periodica in un formato che il
loro gestionale già importa (CSV/Excel con le loro intestazioni). Quasi tutti gli ERP importano
CSV: non serve un progetto, serve un tracciato concordato.

**Fase 3 — integrazione vera.** L'ERP chiama `/api/export`, marca `passata`, il giro si chiude.

Il punto che ti toglie il dubbio: **le fasi 1 e 2 non sono lavoro buttato.** Il JSON è lo stesso
in tutte e tre. Cambia solo chi lo va a prendere — prima una persona, poi un programma.

---

## 7. LA FRATTURA DA NON CREARE

Riassunto della regola, per chi legge solo questa pagina:

| | ERP | MATRICE |
|---|---|---|
| giacenze, carichi, scarichi, valorizzazione, inventari | **sì** | mai |
| codice articolo, costo d'acquisto, fornitore | **sì** | riceve |
| densità, resa, scarto, composizione, regole | riceve | **sì** |
| fabbisogno di commessa, distinta, cicli, prezzo | riceve | **sì** |
| confronto fra resa teorica e consumo reale | fornisce il reale | **espone il confronto** |

L'ultima riga è la più preziosa e nessuno dei due la può fare da solo: l'ERP ha il consumo reale
ma non ha il teorico; Matrice ha il teorico e non vede il reale. Messi insieme rispondono a una
domanda che oggi in azienda non risponde nessuno: **le rese con cui facciamo i prezzi sono vere?**

Questo confronto si **mostra**. Non si scrive mai in magazzino.
