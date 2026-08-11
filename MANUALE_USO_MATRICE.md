# METODO MATRICE — MANUALE D'USO

*Scritto sull'applicazione reale, non su come dovrebbe essere.
Versione 11 agosto 2026.*

---

## COME LEGGERE QUESTO MANUALE

Le sezioni **1–3** servono a chi usa il sistema tutti i giorni.
Le sezioni **4–7** a chi lo governa: regole, anagrafiche, prodotti.
La sezione **8** è l'elenco delle cose che il sistema **non** fa — leggila prima di
prometterle a un cliente.

Ogni volta che compare il segno ⚠ c'è una cosa che si dimentica facilmente e che
costa tempo o errori.

---

# 1 · I TRE RUOLI

Il sistema si comporta in modo diverso a seconda di chi lo sta usando.

| ruolo | cosa vede | come si attiva |
|---|---|---|
| **operatore** | prodotto, prezzo, offerta | è il modo normale |
| **supervisor** | + analisi costi, Officina, anagrafica, mondi | pulsante `ruolo:` in alto |
| **responsabile regole** | + il quadro regole e la loro modifica | pulsante `R ⚿` + password |

⚠ Il pulsante `R ⚿` è **separato dal ruolo**. Un supervisor senza la password non
vede né modifica le regole. La password si cambia nel file: cerca
`REGOLE_PWD_SHA` e sostituisci l'impronta SHA-256 della nuova.

⚠ L'apertura delle regole dura **quanto la scheda del browser**. Chiudendola si
richiude da sola.

---

# 2 · IL GIRO NORMALE — dalla richiesta all'offerta

## 2.1 Scegliere il cliente

In testa all'ordine. Il cliente decide **listino, ricarico, provvigione** e attiva
le regole che lo riguardano.

⚠ Cambiare cliente ricalcola tutto: prezzo, provvigione, regole. Non è un dato
anagrafico, è una variabile del calcolo.

## 2.2 Due strade per configurare

**◇ parti dall'intervista** — il sistema fa le domande e propone il prodotto.
**+ Materasso / + Cuscino / …** — si sceglie il prodotto e si compila a mano.

Entrambe legittime. Ma il prodotto porta scritto quale hai usato:

`◇ da intervista` (petrolio) · `⚠ senza intervista` (ambra)

⚠ La traccia arriva anche all'ERP, nel campo `origine_configurazione`. Chi salta
l'intervista non sbaglia — ma resta scritto, e l'offerta sarà più povera di
informazioni per chi produce.

## 2.3 Registrare

**Registra ordine** congela la commessa. Da lì il prodotto non si modifica più.

⚠ Se una regola di **blocco** è violata, la registrazione è impedita e il motivo
compare in rosso. Non è un avviso da ignorare.

---

# 3 · L'INTERVISTA

## 3.1 Come funziona

Le domande si adattano alle risposte. Sopra le domande, il sistema dichiara sempre
dove sta andando:

- *"Ancora possibili: Materasso · Cuscino · Damiano"* — sta ancora restringendo
- *"Da qui in poi siamo su Materasso — deciso da: …"* — la famiglia si è chiusa
- *"Nessuna famiglia regge queste risposte"* — vicolo cieco, con il motivo

## 3.2 Le risposte «dalla famiglia»

Quando la famiglia si chiude, le domande a cui il prodotto ha già risposto **non
si fanno**. Compaiono nello storico con l'etichetta *dalla famiglia*.

Non è una scorciatoia: chiederle sarebbe una finta scelta.

## 3.3 Le opzioni spente

Un'opzione che violerebbe una regola di prodotto resta visibile ma **spenta**, con
il motivo sotto. Non sparisce, così sai dire al cliente perché no.

⚠ Se **tutte** le opzioni sono spente, il problema è una risposta precedente.
Torna indietro cliccando una risposta nello storico.

## 3.4 Tornare indietro

Ogni risposta nello storico è cliccabile. Ci torni sopra e le successive si
ricalcolano.

## 3.5 Cosa esce alla fine

| caso | cosa succede |
|---|---|
| il prodotto copre le risposte | nasce un prodotto normale |
| copre meno del 70%, o ci sono differenze | nasce una **variante da approvare** (viola) |
| nessuna famiglia regge | scegli tu la linea, nasce comunque |
| una guardia di prodotto è violata | non nasce: si dice cosa lo impedisce |

⚠ Le risposte **restano attaccate al prodotto** e viaggiano fino all'ERP. Si vedono
nel riquadro *DALL'INTERVISTA*, con il conteggio di quante sono state applicate.

Una risposta che non trova un campo dove andare è marcata *senza campo*: resta agli
atti ma non ha effetto. **Se ce ne sono molte, l'intervista è disallineata dal
prodotto** e va corretta.

---

# 4 · LE REGOLE

## 4.1 Dove vivono

| dove | cosa contiene |
|---|---|
| **libreria del prodotto** (Officina) | vincoli fisici: le guardie |
| **Anagrafica ▸ REGOLE** | accordi cliente e regole d'azienda |

## 4.2 Come si scrivono — in italiano

```
se altezza totale > 32 allora ricarico +12%
se cliente è Ospedaliera e area > 3 allora ricarico 1.28
se paese non è Italia allora ricarico +6%
se colore è rosso e cliente è Nautica allora blocca
se materiale è MEMORY allora +8 minuti in Taglio
sempre ricarico +5%
```

**Confronti:** `>` `<` `>=` `<=` · `è` · `non è` · `diverso da`
**Unione:** `e` · `o`
**Senza condizione:** `sempre <azione>`

## 4.3 Cosa può fare una regola

| azione | scrittura | effetto |
|---|---|---|
| prezzo | `prezzo +10%` | maggiora il prezzo finale |
| ricarico | `ricarico 1.28` · `ricarico +12%` | sostituisce il listino |
| provvigione | `provvigione 3%` | fissa la % agente |
| tempo | `+8 minuti in Taglio` | aggiunge minuti a un reparto |
| blocco | `blocca` · `vietato` | impedisce la registrazione |
| avviso | `avviso <testo>` | messaggio, non ferma |

## 4.4 Su cosa vale — le colonne di REGOLE

| cliente | prodotto | vale per |
|---|---|---|
| `*` | `*` | tutti |
| `MEDSRL` | `*` | quel cliente, tutti i prodotti |
| `*` | `materasso` | **tutta la famiglia** materasso |
| `*` | `Materasso Damiano` | solo quel prodotto |

⚠ Il filtro per **famiglia** funziona solo se i prodotti la dichiarano nella
colonna `famiglia` di CARATTERISTICHE.

**Precedenza:** cliente batte azienda — la riga più specifica vince.
**Eccezione assoluta:** le guardie di prodotto non si scavalcano mai.

## 4.5 Il quadro regole

Nel prodotto, con `R ⚿` aperto. Per ogni regola dice:

| stato | significa |
|---|---|
| **attiva** (ambra) | è scattata, con l'effetto accanto |
| **ferma / non violata** | condizione non verificata — è normale per una guardia |
| **annullata** (barrata) | un'altra regola l'ha sovrascritta |
| **NON VALUTABILE** (rossa) | nomina un campo che non esiste: **non scatterà mai** |

E la **bandiera** dice da dove viene: `—` prodotto · `✱` azienda · `nome cliente`.

⚠ Sopra il quadro c'è il **contesto**: `cliente = … · ricarico … · listino …`.
Se una regola non scatta e non capisci perché, la risposta è quasi sempre lì.

⚠ **Le regole si modificano sul posto**: matita ✎ su ogni riga. Quelle
d'anagrafica hanno la matita tratteggiata ⌂ e si correggono in Anagrafica.

---

# 5 · LE ANAGRAFICHE

Dieci tabelle. Si riempiono a mano (`＋ aggiungi riga`, salva automatico) o da CSV.

| tabella | serve a |
|---|---|
| **CLIENTI** | offerta, ERP, **e le sue colonne diventano parole per le regole** |
| **MATERIALI** | densità, natura, costo al kg o al pezzo, scarto |
| **BLOCCHI** | formato del cubotto per materiale |
| **COSTI** | costo blocco e resa in lamine (via storica) |
| **FORNITORI** | il cliente impone il fornitore |
| **LISTINI** | ricarico e provvigione per cliente |
| **REGOLE** | accordi e condizioni |
| **CARATTERISTICHE** | cosa dichiara ogni prodotto — serve all'intervista |
| **COMPOSIZIONE** | da una risposta alla stratigrafia |
| **INTERVISTA** | le domande |

## 5.1 La regola dell'asterisco

`*` vale per tutti. La riga più specifica vince. Vale su ogni colonna filtro.

## 5.2 Le colonne in più non si buttano

Se aggiungi `paese` al CSV clienti, quella colonna **diventa una parola scrivibile
in una regola** la sera stessa. È il meccanismo che rende il sistema estensibile
senza programmatore.

## 5.3 Importare un CSV

⚠ **Ogni file nel suo riquadro.** Se sbagli, il sistema rifiuta e dice quale
colonna manca — leggi il messaggio prima di chiuderlo.

⚠ **L'importazione sostituisce**, non somma.

⚠ **Serve l'intestazione** come prima riga.

⚠ Dopo l'importazione **controlla il numero di righe** accanto al titolo. Se non
è quello atteso, il file non è entrato.

## 5.4 Il pannello DA DICHIARARE

Ambra, nel prodotto. Elenca cosa il prodotto nomina e l'anagrafica non conosce —
con la domanda da girare a chi la sa.

⚠ *"Finché restano aperte, il costo di queste voci è zero — e uno zero non si
vede."* È la frase più importante del sistema.

---

# 6 · I PRODOTTI

## 6.1 Officina

**modifica in Officina** su un prodotto. Da lì:

- **Campi** — cosa il prodotto dichiara. Con `min`/`max` o con valori a scelta.
- **＋ lista FORI** — fori passanti, posizione dal centro in mm
- **＋ ZONE di lavorazione** — zonizzazione lungo la lunghezza
- **Grammatica del codice** — come nasce il codice articolo
- **Regole** — le guardie del prodotto

## 6.2 Le zone — la doppia natura

| campo | natura |
|---|---|
| `da %` `a %` | **scala** con la lunghezza — la zona segue il corpo |
| `lx` `ly` in mm | **NON scala** — la misura la decide l'attrezzatura |
| `mat` + `resa` | se dichiarati, la zona genera righe di distinta |
| `min` + `reparto` | se dichiarati, la zona genera minuti di ciclo |

⚠ Se il materiale è vuoto la zona è **solo lavorazione**: non genera componenti.

## 6.3 Le varianti di cliente

Quando modifichi un prodotto uscendo dalla sua famiglia, compare la proposta di
farne un modello nuovo. Il nome è già proposto — prodotto + cliente.

⚠ La variante **non crea una libreria**: marca la riga. Altrimenti ogni cliente
genererebbe un mondo nuovo in barra.

⚠ Nasce **da approvare**, non confermata.

## 6.4 Il riconoscimento

`GIÀ FATTO — IDENTICO` o `MAI FATTO — PRIMA VOLTA`, con le offerte precedenti.

Il pulsante **riparti** prende **solo la configurazione**, lasciando il cliente e
l'ordine di adesso. Le regole del cliente corrente si riapplicano da sole.

⚠ Cliccare la **riga** invece riapre l'ordine intero, cliente compreso.

---

# 7 · MONDI E AMBIENTE CLIENTE

**manopola attivazione** — accende e spegne i mondi visibili al cliente.

⚠ L'attivazione è **permanente**: sopravvive al ricaricamento.
⚠ Spegnere non cancella. Per cancellare serve un'azione diversa e **non è
reversibile**.
⚠ I **clienti sono dell'azienda**, non del mondo: spegnere un mondo non toglie i
suoi clienti dagli altri.

---

# 8 · QUELLO CHE IL SISTEMA NON FA
### Da leggere prima di prometterlo

| cosa | stato |
|---|---|
| Costo lavorazione con **doppia tariffa** macchina/uomo e operatori | **non c'è** — una tariffa sola per reparto |
| **Provvigione** — noi sul margine, gli ERP spesso sul prezzo finale | **formula diversa**, da allineare |
| **Conflitti fra regole** sullo stesso bersaglio | vince l'ultima, **in silenzio** |
| **Livello ordine**: quantità, lotto, trasporto, scaglioni | **non c'è** — le regole valutano la riga |
| **Misure fuori catalogo** dove il campo è una tendina | non scrivibili |
| **Divieti d'azienda** per cliente o paese | le regole d'azienda maggiorano, non vietano |
| Tempo di lavorazione **dichiarato per componente** con la sua origine | parziale |
| **Zone nel 3D**: disegnate ✔ · nella distinta ✔ · nei cicli ✔ | completo |
| **Distinta a livelli** (semilavorati con codice proprio) | non c'è |
| **Tolleranze** sulle misure | non c'è |
| **Magazzino, giacenze, approvvigionamento** | **fuori perimetro** — è dell'ERP |

---

# 9 · I DIECI ERRORI CHE COSTANO PIÙ TEMPO

1. **Importare un CSV nel riquadro sbagliato** — leggi il messaggio di rifiuto.
2. **Non controllare il numero di righe** dopo un'importazione.
3. **CSV senza intestazione** — viene rifiutato.
4. **Scrivere una regola su un campo che non esiste** — resta `NON VALUTABILE`.
5. **Cercare una regola che non scatta** senza guardare il contesto sopra il quadro.
6. **Dimenticare la colonna `famiglia`** e poi stupirsi che il filtro non filtri.
7. **Ricaricare la pagina** aspettandosi il file nuovo senza `Ctrl+F5`.
8. **Lasciare aperto DA DICHIARARE** e fidarsi dei costi.
9. **Confondere spegnere con cancellare** un mondo.
10. **Modificare un prodotto dopo l'intervista** senza leggere gli scostamenti.

---

# 10 · IL COLLEGAMENTO CON IL MANUALE OPERATIVO

Questo manuale spiega **come si usa**. Il manuale operativo dovrà spiegare
**chi fa cosa, e quando**. I punti in cui i due si toccano:

**Chi tiene la password delle regole.** Un solo responsabile. Se gira fra dieci
persone, il pulsante non separa più niente.

**Chi approva le varianti.** Nascono `da approvare` e nessuno oggi le approva:
serve una persona e un momento.

**Chi mantiene l'intervista.** Le domande invecchiano. Senza revisione periodica
diventano un modulo da compilare in fretta scegliendo sempre la prima opzione.

**Chi svuota DA DICHIARARE.** Ogni voce è una domanda a qualcuno. Se non ha un
destinatario, resta lì e i costi restano a zero.

**Chi decide quando un prodotto nuovo entra a catalogo.** Le varianti si
accumulano: a un certo punto una di esse è un prodotto, e va promossa.

**Cosa si dice al cliente sui limiti.** La sezione 8 non è un documento interno:
è la base di quello che si promette e di quello che non si promette.
