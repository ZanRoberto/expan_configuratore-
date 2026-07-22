-- ═══════════════════════════════════════════════════════════════
-- METODO MATRICE — schema multi-tenant  (PostgreSQL)
-- Gerarchia:  partner → azienda → mondo → ordini
-- Un solo database. Separazione per CHIAVI, non per file.
-- ═══════════════════════════════════════════════════════════════

-- ── 1. PARTNER ────────────────────────────────────────────────
-- Chi rivende/gestisce piu' aziende. Tu stesso sei un partner.
CREATE TABLE partner (
  id          TEXT PRIMARY KEY,              -- es. 'tecnaria', 'albaconsulting'
  nome        TEXT NOT NULL,
  attivo      BOOLEAN NOT NULL DEFAULT TRUE,
  creato      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 2. AZIENDA ────────────────────────────────────────────────
-- Il cliente vero. Qui si separano i dati.
CREATE TABLE azienda (
  id          TEXT PRIMARY KEY,              -- es. 'materassi_rossi', 'essart'
  partner_id  TEXT NOT NULL REFERENCES partner(id),
  nome        TEXT NOT NULL,
  marchio     TEXT,                          -- logo/intestazione mostrata
  attiva      BOOLEAN NOT NULL DEFAULT TRUE,
  creata      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_azienda_partner ON azienda(partner_id);

-- ── 3. MONDO ──────────────────────────────────────────────────
-- Il MONDO DI PRODOTTO (materassi, tende, portoni).
-- NON e' un cliente: e' un ambiente dentro un'azienda.
CREATE TABLE mondo (
  id            BIGSERIAL PRIMARY KEY,
  azienda_id    TEXT NOT NULL REFERENCES azienda(id),
  slug          TEXT NOT NULL,               -- 'materassi', 'tende_essart'
  titolo        TEXT NOT NULL,
  motore        TEXT NOT NULL,               -- glb | serie | dxf | forma | strati
  definizione   JSONB NOT NULL,              -- campi, regole, leggi di costo
  aggiornato    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (azienda_id, slug)
);

-- ── 4. ANAGRAFICA ─────────────────────────────────────────────
-- Come oggi, ma legata all'azienda per chiave esterna vera.
CREATE TABLE anagrafica (
  azienda_id  TEXT NOT NULL REFERENCES azienda(id),
  tipo        TEXT NOT NULL,                 -- fornitori | costi | listini | mondi
  dati        JSONB NOT NULL,
  aggiornato  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (azienda_id, tipo)
);

-- ── 5. CONTATORI ──────────────────────────────────────────────
-- ⚠ CAMBIO CHIAVE: il contatore e' PER AZIENDA, non globale.
-- Cosi' ogni azienda ha la SUA serie di numerazione.
CREATE TABLE contatori (
  azienda_id  TEXT NOT NULL REFERENCES azienda(id),
  chiave      TEXT NOT NULL,                 -- 'offerta' | 'ordine'
  anno        INTEGER NOT NULL,
  valore      INTEGER NOT NULL,
  PRIMARY KEY (azienda_id, chiave, anno)
);

-- ── 6. ORDINI / OFFERTE ───────────────────────────────────────
-- ⚠ AGGIUNTO azienda_id: oggi manca del tutto.
CREATE TABLE ordine (
  id           BIGSERIAL PRIMARY KEY,
  azienda_id   TEXT NOT NULL REFERENCES azienda(id),
  mondo_id     BIGINT REFERENCES mondo(id),
  numero       TEXT NOT NULL,                -- OFF-2026-0001 (unico DENTRO l'azienda)
  data         DATE NOT NULL DEFAULT CURRENT_DATE,
  cliente_cod  TEXT,
  stato        TEXT NOT NULL DEFAULT 'bozza',-- bozza | confermata | passata
  creato_da    TEXT,
  payload      JSONB,
  creato       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (azienda_id, numero)
);
CREATE INDEX ix_ordine_azienda_data ON ordine(azienda_id, data);
CREATE INDEX ix_ordine_stato        ON ordine(azienda_id, stato);

-- ── 7. RIGHE ORDINE ───────────────────────────────────────────
-- Serve per la PRODUTTIVITA': senza righe si conta solo il numero
-- di ordini, non i pezzi ne' il valore.
CREATE TABLE ordine_riga (
  id          BIGSERIAL PRIMARY KEY,
  ordine_id   BIGINT NOT NULL REFERENCES ordine(id) ON DELETE CASCADE,
  azienda_id  TEXT NOT NULL REFERENCES azienda(id),  -- ridondante di proposito: filtro diretto
  mondo_id    BIGINT REFERENCES mondo(id),
  codice      TEXT,
  descrizione TEXT,
  qta         NUMERIC(12,3) NOT NULL DEFAULT 1,
  costo       NUMERIC(12,2) NOT NULL DEFAULT 0,
  prezzo      NUMERIC(12,2) NOT NULL DEFAULT 0,
  cfg         JSONB
);
CREATE INDEX ix_riga_azienda ON ordine_riga(azienda_id);

-- ═══════════════════════════════════════════════════════════════
-- PRODUTTIVITA' — le query che oggi NON si possono fare
-- ═══════════════════════════════════════════════════════════════

-- Produttivita' per azienda di un partner (la tua richiesta):
--   SELECT a.nome,
--          COUNT(DISTINCT o.id)            AS ordini,
--          SUM(r.qta)                      AS pezzi,
--          SUM(r.prezzo * r.qta)           AS fatturato,
--          SUM((r.prezzo - r.costo) * r.qta) AS margine
--   FROM azienda a
--   JOIN ordine o      ON o.azienda_id = a.id
--   JOIN ordine_riga r ON r.ordine_id  = o.id
--   WHERE a.partner_id = 'albaconsulting'
--     AND o.data >= date_trunc('year', CURRENT_DATE)
--   GROUP BY a.nome ORDER BY fatturato DESC;

-- Confronto tra mondi (materassi vs tende) dentro un'azienda:
--   SELECT m.titolo, COUNT(*) , SUM(r.prezzo*r.qta)
--   FROM ordine_riga r JOIN mondo m ON m.id = r.mondo_id
--   WHERE r.azienda_id = 'essart' GROUP BY m.titolo;

-- ═══════════════════════════════════════════════════════════════
-- ISOLAMENTO A PROVA DI DIMENTICANZA (Row Level Security)
-- Senza questo, un WHERE azienda_id dimenticato = dati di un
-- cliente mostrati a un altro. Con questo, il database rifiuta.
-- ═══════════════════════════════════════════════════════════════
-- ALTER TABLE ordine       ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE ordine_riga  ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE anagrafica   ENABLE ROW LEVEL SECURITY;
--
-- CREATE POLICY p_ordine ON ordine
--   USING (azienda_id = current_setting('app.azienda_id', true));
--
-- Nel codice, a ogni richiesta:
--   SET LOCAL app.azienda_id = 'essart';
