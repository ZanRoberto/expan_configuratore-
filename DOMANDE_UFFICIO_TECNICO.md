# DOMANDE ALL'UFFICIO TECNICO

*Costruite leggendo `Cliente1 - Osiris Bgn h20.xlsx` e il disegno del materasso.
Ogni domanda nasce da un dato reale, non da curiosità.
Versione 1 — 8 agosto 2026. Da aggiornare a ogni risposta.*

**Come si usa:** la colonna «perché la chiedo» non si legge ad alta voce — serve a te
per capire cosa fare della risposta. Se una risposta chiude una domanda, si segna
qui sotto e non si richiede mai più.

---

## A · LE ZONE DI LAVORAZIONE
*Dal disegno: il torba ha zig-zag alle fasce e due inserti al centro.*

**A1.** Le fasce zig-zag e gli inserti stanno sempre nella stessa posizione, o si
spostano a seconda della lunghezza del materasso?
> *Perché: decide se la posizione è una percentuale o un numero fisso. Le zone
> sembrano anatomiche (spalle, lombare, piedi), quindi dovrebbero scalare.*

**A2.** Su un 190 e su un 200 gli inserti restano 12×4,5, o cambiano anche loro?
> *Perché: la misura dell'inserto sembra imposta dall'attrezzatura (dal blocco
> 204×105 ne escono 15×20=300 con 6 mm di sfrido). Se confermano che non cambia,
> posizione e dimensione hanno nature diverse — ed è la cosa da non sbagliare.*

**A3.** Gli inserti verdi sono tagliati dentro il torba, o sono pezzi separati che
vengono alloggiati in una sede?
> *Perché: se sono pezzi, generano righe di distinta proprie e un ciclo di
> incollaggio. Se sono lavorazione, no. Nella distinta hanno una riga a parte —
> ma va confermato.*

**A4.** Quante zone diverse arrivate ad avere su un modello? C'è un massimo?

**A5.** Le zone cambiano da un modello all'altro, o esistono profili ricorrenti che
riusate?
> *Perché: se ci sono profili standard, diventano una libreria di profili invece
> che una lista da riscrivere ogni volta.*

---

## B · IL BLOCCO E LA RESA
*Dalla formula: peso = volume × densità × (1+scarto) ÷ qdr_pz × nr_pz*

**B1.** Il `qdr pz` — quante lastre escono da un blocco — lo calcolate voi o ve lo
dà il fornitore?

**B2.** Cambia con l'orientamento del taglio? Lo stesso blocco reso per il lungo o
per il traverso dà lo stesso numero?

**B3.** Avete due scarti, uno generale e uno di riga. Cosa distingue i due?

**B4.** Il blocco 204×105×82 è sempre quello, o ogni materiale ha il suo formato?

**B5.** Cosa succede al pezzo di blocco che avanza? Rientra da qualche parte o è
perso?

---

## C · I CICLI E I TEMPI
*Dal foglio: costo = macchina × tempo ÷ pezzi + uomo × tempo ÷ pezzi × operatori*

**C1.** Il costo al minuto delle macchine — 0,62 la sagomatrice, 0,099 lo
spaccablocco — come lo ricavate? Ammortamento, energia, manutenzione?

**C2.** La manodopera è sempre 25 €/ora per ogni reparto. È una media di comodo o
è davvero così?
> *Perché: se sono tariffe diverse per reparto, il modello va per reparto.*

**C3.** Il numero di operatori cambia mai per la stessa lavorazione? La bugnatura
è sempre a 3?

**C4.** L'imballo è «1 minuto ogni 3 pezzi». Quindi alcuni tempi sono a lotto e non
a pezzo — quali altri lo sono?
> *Perché: è il primo punto in cui compare la quantità. Se ce ne sono altri,
> il livello ordine diventa urgente.*

**C5.** Il tempo di attesa — riposo della schiuma, sfiato — lo contate da qualche
parte, o è fuori dal costo?
> *Perché: è un tempo che occupa spazio, non persone. Se lo contassero come
> manodopera il costo sarebbe assurdo, quindi presumo sia fuori. Da confermare.*

---

## D · LA SIMULAZIONE DELLA SAGOMATRICE
*Dalle annotazioni: «Mediano sup 8/85% 480s /2 piano > 4»*

**D1.** Il software della macchina cosa vi restituisce esattamente? Solo secondi, o
anche percorso, numero di attacchi, consumo lama?

**D2.** «/2 piano» vuol dire che tagliate due pezzi sovrapposti in un colpo?

**D3.** Quei secondi valgono per il pezzo singolo o per il nesting completo della
lastra?
> *Perché: se è per nesting, il tempo del pezzo dipende da quanti ne tagli
> insieme — cioè dal lotto, di nuovo.*

**D4.** Se cambia la sagoma di poco — un inserto spostato di 2 cm — rifate la
simulazione o stimate a occhio?

**D5.** Il DXF da dove nasce? Lo disegnate voi o arriva dal cliente?

**D6.** Quel numero, una volta trascritto nel foglio, resta lì per sempre? Cosa
succede se cambiate macchina o velocità di taglio?
> *Perché: è conoscenza che oggi vive in una nota di testo. Se cambia la
> macchina, centinaia di file restano con i tempi vecchi.*

---

## E · IL TRASPORTO
*Dalla nota: camion 141 mc, pezzo 200×80×20, 374 pezzi, 2.868 € → 7,67 €/pz*

**E1.** Il costo per pezzo lo ricalcolate a ogni ordine o resta quello per tutto
l'anno?

**E2.** Se il camion parte mezzo vuoto, il costo per pezzo raddoppia. Chi se lo
prende?

**E3.** I materassi si comprimono o arrotolano per il trasporto? Cambia quanti ne
entrano?

**E4.** Le tariffe al km e i volumi dei mezzi stanno da qualche parte, o si va a
memoria?
> *Perché: se sono dati, il trasporto si calcola invece di stimarlo. È una delle
> cose che possiamo fare molto meglio di un foglio.*

---

## F · PREZZO, MARGINE, PROVVIGIONE
*Dal foglio: sp.generali 70,44% sui materiali · provvigione totale÷(1−3%)*

**F1.** Le spese generali sono il 70,44% sui soli materiali, non sul costo totale.
È una scelta o è come è sempre stato?

**F2.** Quel numero ogni quanto lo rivedete, e chi lo decide?

**F3.** La provvigione la calcolate sul prezzo finale, non sul margine. È così per
tutti gli agenti?
> *Perché: noi oggi la calcoliamo sul margine. Su questo esempio loro fanno 6,00 €,
> noi ne faremmo circa 2,50. Va allineato, e la scelta è loro.*

**F4.** Le altre misure hanno il prezzo scalato in proporzione all'area — 200×200
costa 2,5 volte 80×200. Ma il costo reale scala davvero così?
> *Perché: cambia la resa del blocco, cambiano i tempi di manipolazione, cambia
> l'imballo. Loro lo sanno di sicuro. Questa è la domanda che apre la porta:
> avendo la distinta vera per ogni misura, noi possiamo calcolarlo invece di
> scalarlo.*

**F5.** Sui listini materiali ne avete quattro (L2302pr, L2307, L2309, L2402pr).
Come si sceglie quale usare?

---

## G · IL PRODOTTO
*Dal disegno: LATO FIRM sopra, LATO SOFT sotto, h nominale 20 / reale 19,8*

**G1.** Il materasso ha due lati diversi. Come lo dichiarate in produzione — c'è
un'etichetta, una cucitura, un verso obbligato?

**G2.** Testa e piede sono distinguibili sul pezzo finito?

**G3.** Il 20 nominale contro il 198 reale: la differenza da dove nasce?
Compressione degli strati incollati?

**G4.** Che tolleranza accettate sulle misure finali?
> *Perché: è la domanda che un capo reparto si aspetta. Oggi il nostro modello
> tratta i numeri come esatti.*

**G5.** «Con incastro» — il ricamo fra torba e sabbia. È una lavorazione a sé o è
la forma del taglio?

---

## H · ANAGRAFICHE E FLUSSO
**H1.** Il listino materiali da 2.396 righe da dove arriva? Esportato dall'ERP o
tenuto a mano?

**H2.** Ogni quanto lo aggiornate, e come si accorgono i fogli già fatti che un
prezzo è cambiato?
> *Perché: sospetto che non se ne accorgano affatto. È l'argomento più forte a
> favore di una sorgente unica.*

**H3.** Quanti file scheda avete in circolazione, più o meno?

**H4.** Se cambia la tariffa oraria, cosa succede a quei file?

**H5.** I clienti nell'ERP hanno un codice stabile? La P.IVA è affidabile come
chiave?

---

## RISPOSTE OTTENUTE

*Da riempire. Quando una domanda è chiusa, la risposta va qui con la data — e la
domanda sopra si segna come risolta invece di essere cancellata, così resta la
traccia di cosa si è chiesto e quando.*

| # | Data | Risposta | Cosa cambia da noi |
|---|---|---|---|
| | | | |

---

## LE TRE DOMANDE CHE VALGONO PIÙ DELLE ALTRE

Se il tempo è poco, queste:

**A2** — se la dimensione dell'inserto non scala, la doppia natura è confermata e
il modello delle zone si costruisce senza rischio.

**D3** — se i tempi della sagomatrice sono per nesting e non per pezzo, allora
tutto il calcolo dipende dal lotto, e il livello ordine diventa la priorità
assoluta invece che il quarto punto della lista.

**F4** — se ammettono che lo scaling per area è un'approssimazione, hanno appena
descritto da soli il motivo per cui serve un generatore.
