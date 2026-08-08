# MOTORE REGOLE — Metodo Matrice

*Stato al 7 agosto 2026. Verificato sul codice, non a memoria.*

---

## Il principio

Una regola è una frase in italiano che collega **una condizione** a **un'azione**.

```
se <condizione> allora <azione>
```

Il motore non ha un vocabolario chiuso. Le parole utilizzabili non sono scritte
nel codice: si leggono dai dati dichiarati. Aggiungi un campo in Officina o una
colonna in anagrafica, e quella parola diventa scrivibile la sera stessa.

Questo è il punto che distingue Matrice da un configuratore a listino: le regole
non sono state programmate, sono state **dichiarate**.

---

## 1 · CHI CONDIZIONA — le sorgenti

Una condizione può nominare qualunque cosa provenga da queste sorgenti.

### 1.1 Campi del prodotto — APERTA

Tutto ciò che dichiari in Officina. Lunghezza, spessore, materiale, colore,
numero onde, forma, e qualunque campo aggiungerai domani.

> `se coefficente non è TT1 allora blocca`

Il vocabolario a video mostra i campi di **quella** libreria, non un elenco fisso.

### 1.2 Cliente e sua anagrafica — APERTA

Il nome del cliente, e **ogni colonna dell'anagrafica clienti**. Aggiungi la
colonna `paese` al CSV e la parola esiste da subito.

> `se paese è Germania allora ricarico +5%`
> `se cliente è Ospedaliera e superficie riposo > 3 allora ricarico 1.28`

Disponibili sempre: `cliente`, `codice cliente`, `agente`, `listino`, `ricarico`.

### 1.3 Grandezze calcolate — chiusa, per natura

Nascono dal calcolo del prodotto: `area`, `altezza totale`, `molle`,
`numero strati`. Non si aggiungono da interfaccia perché non sono dati: sono
risultati.

### 1.4 Composizione — **MANCANTE**

Una regola non sa leggere i singoli strati. Non è scrivibile:

> ~~`se uno strato è MEMORY allora ...`~~

Il dato c'è nello scope, mancano le funzioni per interrogarlo. È il primo buco.

### 1.5 Ordine e commessa — **MANCANTE**

Le regole girano **sulla riga**, mai sull'ordine. Non sono esprimibili:

> ~~`se totale ordine > 5000 allora sconto 3%`~~
> ~~`se quantità > 20 allora ricarico 1.15`~~

È il buco più grande. Trasporto, scaglioni, minimi di fornitura e attrezzaggio
a lotto vivono tutti qui.

---

## 2 · COSA PRODUCONO — i sette bersagli

| Bersaglio | Effetto | Grammatica italiana |
|---|---|---|
| **prezzo** | maggiora il prezzo finale in % | `prezzo +10%` |
| **ricarico** | decide il moltiplicatore, sostituendo il listino | `ricarico 1.28` · `ricarico +12%` |
| **provvigione** | fissa la % dell'agente | `provvigione 3%` |
| **tempo** | aggiunge minuti a un reparto | `+8 minuti in Taglio` |
| **blocco** | impedisce la registrazione dell'ordine | `blocca` · `vietato` |
| **avviso** | messaggio a video, non ferma nulla | `avviso <testo>` |
| **componente** | aggiunge una riga di distinta | *solo dichiarativa* |
| **codice** | aggiunge un segmento al codice articolo | *solo dichiarativa* |

Gli ultimi due funzionano nel motore ma **non hanno ancora una frase in italiano**:
si scrivono in forma strutturata.

---

## 3 · LA GRAMMATICA

**Confronti** — `>` `<` `>=` `<=` · `è` · `non è` · `diverso da`
**Unione** — `e` · `o`
**Vincolo** — `<campo> deve essere >= <valore>`

I messaggi calcolano: `${hTot}` diventa 34, `${cliente}` diventa il nome vero.
Non è una frase preparata, è la macchina che dichiara il numero che ha usato.

**Attenzione:** il confronto di uguaglianza è **parziale**, non esatto. `TT1`
corrisponde anche a `TT10`. Serve per far funzionare `cliente è Ospedaliera`
quando in anagrafica c'è "Ospedaliera Med S.r.l.", ma sulle sigle numerate è una
trappola. Da correggere: esatto sui campi a valori dichiarati, parziale sui nomi
liberi.

---

## 4 · CONFLITTI — difetto da correggere

Due regole che agiscono sullo stesso bersaglio con valori diversi: **oggi vince
l'ultima che scatta, in silenzio**.

È un difetto, non una scelta. Il prezzo cambia e nessuno sa perché. Con due
regole non si nota; con venti è un disastro annunciato.

**Correzione prevista, in due tempi:**

1. **Il conflitto diventa un errore.** Due ricarichi incompatibili fermano il
   calcolo e lo dicono: *"le regole 3 e 7 danno ricarichi diversi"*. Meglio un
   blocco che obbliga a decidere, che un prezzo sbagliato scoperto in fattura.
2. **Rango dichiarato.** Ogni regola porta una priorità scritta, non la
   posizione nell'elenco. Il conflitto si risolve per rango, e l'interfaccia
   mostra chi vince su chi.

---

## 5 · QUADRO DELLE REGOLE — da costruire

Il vocabolario dice cosa *puoi* scrivere. Nessuno dice cosa *hai già* scritto.
A venti regole serve un quadro che mostri, per ognuna:

- **stato adesso** — scattata o no sulla configurazione aperta, e con che effetto
- **sovrascritture** — quale regola annulla quale
- **bersaglio** — raggruppate per effetto, non in elenco piatto
- **regole morte** — quelle che non scattano mai perché la condizione è
  impossibile o il campo che nominano non esiste più
- **firma** — chi l'ha scritta e quando

L'ultima sembra burocrazia e non lo è: una regola sul prezzo è una decisione
commerciale. Fra sei mesi, davanti a un margine sbagliato, la domanda non sarà
"che regola è" ma "chi l'ha messa". Si aggiunge adesso quasi gratis; dopo non si
recupera.

---

## 6 · IL CONFINE CON L'ERP

Le regole **non escono**. L'ERP non riceve né la condizione, né l'espressione, né
il testo italiano: sono know-how.

Riceve **l'effetto e la sua motivazione in chiaro**:

```json
{
  "prezzo_unitario": 253.44,
  "prezzo_origine": "regola",
  "ricarico_applicato": 1.28,
  "ricarico_motivo": "Accordo Ospedaliera su misure grandi",
  "listino": "L1"
}
```

Il know-how resta dentro, la tracciabilità va fuori. Fra sei mesi quel 253,44 sa
ancora dire perché.

---

## 7 · ORDINE DI LAVORO

1. **Conflitti come errore** — è già in produzione, va corretto per primo
2. **Anagrafica materiali con colonna `natura`** — sblocca costi veri, molle
   dichiarate anziché indovinate, e il terzo livello di distinta
3. **Funzioni sulla composizione** — regole che leggono gli strati
4. **Livello ordine** — quantità, totale commessa, trasporto, scaglioni
5. **Quadro delle regole** — manutenzione a venti regole
6. **Confronto esatto vs parziale** — tocca tutte le regole esistenti, va fatto
   con calma
