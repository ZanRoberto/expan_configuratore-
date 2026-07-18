# PORTONE SEZIONALE — definizione libreria + fix distinta

Stato validato sui dati veri (GLB `portone_sezionale_garage_componenti`):
- legge di istanziazione: girata su 5 scenari, regge
- distinta che cresce con N: girata, regge
- **da fare, non testabile fuori dal browser:** innesto rendering 3D in `applicaMisureGLB3`

Il GLB ha 69 componenti. Il motore li ha classificati DA SOLO dai bbox:
- **serie per-pannello** (crescono con N): `pannello_*`, `rinforzo_orizzontale_*`, `rullo_sinistro_*`, `rullo_destro_*`
- **serie cerniere** (fra i pannelli, (N-1)×3): `cerniera_rNN_*`
- **si allungano in Z** (7): guide verticali, montanti, guarnizioni laterali, rinforzo centrale
- **traslano in cima** (26): curve, guide orizzontali, molla, albero, motore, staffe, tamburi, ...
- **restano fermi** (4): maniglia, guarnizione inferiore, fotocellule

Passo nativo del GLB misurato = **480 mm** (non 500): è una semplificazione di GPT →
NON scalare il nativo, ISTANZIARE col passo che dà l'utente.

---

## ① DEFINIZIONE LIBRERIA PORTONE (formato Narratore, esteso con `qta`)

```json
{
  "nome": "Portone sezionale da garage",
  "motore": "glb",
  "campi": [
    {"id":"altezza","ruolo":"altezza","label":"Altezza vano (mm)","tipo":"num","std":["2500","3000","2000"],"min":1500,"max":6000},
    {"id":"passo","ruolo":"passo","label":"Passo pannello (mm)","tipo":"num","std":["500","400","600","610"],"min":300,"max":750},
    {"id":"larghezza","ruolo":"larghezza","label":"Larghezza vano (mm)","tipo":"num","std":["3000","2500","4000"],"min":2000,"max":6000},
    {"id":"colore","ruolo":"colore","label":"Colore","tipo":"scelta","opz":["bianco","grigio","antracite"],"cod":{"bianco":"BIA","grigio":"GRI","antracite":"ANT"}},
    {"id":"motor","label":"Motorizzato","tipo":"scelta","opz":["No","Sì"],"cod":{"No":"X","Sì":"M"}}
  ],
  "regoleText": [
    "l'altezza è libera a passi di pannello: N = ceil(altezza / passo)",
    "se il vano non è multiplo del passo, l'ultimo pannello è di compenso (tagliato)",
    "se motor = 'Sì' allora il motoriduttore entra in distinta"
  ],
  "normativa": ["UNI EN 13241 (porte e cancelli industriali/residenziali)", "UNI EN 12604/12453 (sicurezza)"],
  "distinta": [
    {"cod":"PRT-PAN-${VAR(colore)}", "nome":"Pannello coibentato ${colore}", "um":"pz", "qta":"ceil(altezza/passo)"},
    {"cod":"PRT-RINF",  "nome":"Rinforzo orizzontale",           "um":"pz", "qta":"ceil(altezza/passo)"},
    {"cod":"PRT-RUL",   "nome":"Rullo guida",                    "um":"pz", "qta":"ceil(altezza/passo)*2"},
    {"cod":"PRT-CER",   "nome":"Cerniera",                       "um":"pz", "qta":"(ceil(altezza/passo)-1)*3"},
    {"cod":"PRT-GUI",   "nome":"Guida verticale L=${altezza}mm", "um":"pz", "qta":"2",  "prezzo":"altezza/1000*14"},
    {"cod":"PRT-MOT",   "nome":"Motoriduttore",                  "um":"pz", "qta":"1",  "se":"motor=='Sì'"},
    {"cod":"PRT-MAN",   "nome":"Maniglia",                       "um":"pz", "qta":"1"}
  ]
}
```

Nota: `qta` e `prezzo` sono ESPRESSIONI, valutate da `EX()` sullo scope dei campi
(che ha già `ceil/floor/round/max/min`). `${VAR(colore)}` = sigla per il codice,
`${colore}` = valore esteso per la descrizione. `se` = condizione di comparsa (già gestita, riga 1510).

---

## ② FIX-2618 — non azzerare le quantità che la voce porta

Punto: costruzione libreria dal draft del Narratore.

PRIMA (azzera tutto a 1):
```js
componenti:(d.distinta||[]).map(function(v){return {cod:v.cod,nome:v.nome,um:v.um,qta:'1',prezzo:'0'};}),
```

DOPO (rispetta l'espressione se c'è):
```js
componenti:(d.distinta||[]).map(function(v){return {
  cod:v.cod, nome:v.nome, um:v.um,
  qta:(v.qta!=null && v.qta!=='')?String(v.qta):'1',
  prezzo:(v.prezzo!=null && v.prezzo!=='')?String(v.prezzo):'0',
  se:(v.se!=null && v.se!=='')?v.se:undefined
};}),
```

Una riga logica. Non tocca il resto del motore. Da sola, sblocca la distinta-che-cresce
per QUALSIASI libreria, non solo il portone.

---

## ③ FIX-PROMPT — insegnare al Narratore a CONTARE, non solo a nominare

Nel `PROMPT_NARRATORE`, lo schema della distinta oggi è `[{cod, nome, um}]`.
Va esteso a `[{cod, nome, um, qta, prezzo?, se?}]` con l'istruzione:

> La distinta deve portare la QUANTITÀ come espressione dei campi, non un numero fisso.
> Per un prodotto in serie il numero dei pezzi nasce dai campi: es. un portone sezionale
> con campi `altezza` e `passo` ha `qta: "ceil(altezza/passo)"` per il pannello,
> `"(ceil(altezza/passo)-1)*3"` per le cerniere. Se una voce compare solo in un caso,
> usa `se` (es. `"se":"motor=='Sì'"`). Se non dipende da nulla, `qta:"1"`.

Questo è il pezzo che rende vero il "da una descrizione nasce tutto":
il Narratore smette di consegnare distinte piatte e consegna distinte che RESPIRANO coi campi.

---

## ④ RENDERING (da scrivere, lo testi TU nel browser)

Innesto in `applicaMisureGLB3`: prima dei casi A (corpo+tappi) e B (scala d'insieme),
un CASO 0 — se i componenti hanno serie `_NN`, ISTANZIALE invece di scalare il modello
in altezza. Guide verticali → allunga in Z. Testata (curve/molla/motore/staffe) → trasla
in Z di (altezza − altezza_nativa). ~50–80 righe Three.js. WebGL non gira fuori dal browser.
