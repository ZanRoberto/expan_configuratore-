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
