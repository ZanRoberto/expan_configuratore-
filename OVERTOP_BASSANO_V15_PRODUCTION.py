#!/usr/bin/env python3
"""
OVERTOP BASSANO V14 PRODUCTION - FULL BUILD
BOT TRADING COMPLETO INTEGRATO - PRODUCTION READY
Con Oracolo 2.0: memoria multi-dimensionale, context-matching,
capsule auto-generative, duration memory, post-trade tracker.
PAPER TRADE: imposta PAPER_TRADE = True per test sicuro
"""

import json
import websocket
import threading
import time
import hashlib
import operator
import sqlite3
import os
from datetime import datetime
from collections import deque, defaultdict
import logging
import sys

# ===========================================================================
# [CFG]️  CONFIGURAZIONE GLOBALE
# ===========================================================================

# --- PAPER TRADE FLAG -------------------------------------------------------
# True  = simula tutto, zero ordini reali su Binance → usa per testare
# False = ordini reali → SOLO dopo paper test soddisfacente
PAPER_TRADE = True

# --- SEED SCORER ------------------------------------------------------------
SEED_ENTRY_THRESHOLD = 0.45   # soglia minima per entrare

# --- DIVORCE TRIGGERS -------------------------------------------------------
DIVORCE_DRAWDOWN_PCT   = 3.0  # % drawdown dal massimo → trigger 3
DIVORCE_FP_DIVERGE_PCT = 0.50 # divergenza fingerprint > 50% → trigger 4
DIVORCE_MIN_TRIGGERS   = 2    # quanti trigger devono scattare per uscita immediata

# --- DATABASE ----------------------------------------------------------------
DB_PATH        = os.environ.get("DB_PATH", "/home/app/data/trading_data.db")
NARRATIVES_DB  = os.environ.get("NARRATIVES_DB", "/home/app/data/narratives.db")

# --- BINANCE -----------------------------------------------------------------
SYMBOL         = "SOLUSDC"
BINANCE_WS_URL = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@aggTrade"

# ===========================================================================
# LOGGING
# ===========================================================================

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

# ===========================================================================
# OPERATORS FOR CAPSULE RUNTIME
# ===========================================================================

OPS = {
    '>':      operator.gt,
    '>=':     operator.ge,
    '<':      operator.lt,
    '<=':     operator.le,
    '==':     operator.eq,
    '!=':     operator.ne,
    'in':     lambda a, b: a in b,
    'not_in': lambda a, b: a not in b,
}

# ===========================================================================
# STABILITY TELEMETRY - LOGGING PASSIVO, ZERO LOGICA
# Solo osserva. Non decide. Non modifica. Non ottimizza.
# ===========================================================================

class StabilityTelemetry:
    """Registra ogni decisione, flip, cambio parametro. Solo logging.
    
    VINCOLI OBBLIGATORI:
    1. Ogni evento ha SEMPRE: ts, event_type, regime, direction, open_position
    2. flip/param_change/trade_close/regime_change hanno anche snapshot:
       active_threshold, drift, macd, trend, volatility, bridge_reason
    """

    def __init__(self):
        self._start_time = time.time()
        self._events = []    # TUTTI gli eventi, schema uniforme

    def _base(self, event_type, regime, direction, open_position):
        """Campi minimi obbligatori su OGNI evento."""
        return {
            'ts': time.time(),
            'event_type': event_type,
            'regime': regime,
            'direction': direction,
            'open_position': open_position,
        }

    def _snapshot(self, active_threshold, drift, macd, trend, volatility, bridge_reason=None):
        """Snapshot di contesto per eventi strutturali."""
        return {
            'active_threshold': active_threshold,
            'drift': round(drift, 5) if drift is not None else 0,
            'macd': round(macd, 5) if macd is not None else 0,
            'trend': trend,
            'volatility': volatility,
            'bridge_reason': bridge_reason,
        }

    # -- EVENTI CON SNAPSHOT -----------------------------------------------

    def log_direction_flip(self, old_dir, new_dir, regime, direction, open_position,
                           active_threshold, drift, macd, trend, volatility, bridge_reason=None):
        e = self._base("DIRECTION_FLIP", regime, direction, open_position)
        e['old_direction'] = old_dir
        e['new_direction'] = new_dir
        e.update(self._snapshot(active_threshold, drift, macd, trend, volatility, bridge_reason))
        self._events.append(e)

    def log_direction_hold(self, bearish_signals, regime, direction, open_position,
                           active_threshold, drift, macd, trend, volatility):
        e = self._base("DIRECTION_HOLD", regime, direction, open_position)
        e['bearish_signals'] = bearish_signals
        e.update(self._snapshot(active_threshold, drift, macd, trend, volatility))
        self._events.append(e)

    def log_param_change(self, param, old_val, new_val, regime, direction, open_position,
                         active_threshold, drift, macd, trend, volatility, bridge_reason=None):
        e = self._base("PARAM_CHANGE", regime, direction, open_position)
        e['param'] = param
        e['old_value'] = old_val
        e['new_value'] = new_val
        e.update(self._snapshot(active_threshold, drift, macd, trend, volatility, bridge_reason))
        self._events.append(e)

    def log_param_rejected(self, param, value, reason, regime, direction, open_position,
                           active_threshold, drift, macd, trend, volatility):
        e = self._base("PARAM_REJECTED", regime, direction, open_position)
        e['param'] = param
        e['rejected_value'] = value
        e['reject_reason'] = reason
        e.update(self._snapshot(active_threshold, drift, macd, trend, volatility))
        self._events.append(e)

    def log_trade_close(self, trade_direction, pnl, is_win, exit_reason, duration,
                        regime, direction, open_position,
                        active_threshold, drift, macd, trend, volatility):
        e = self._base("TRADE_CLOSE", regime, direction, open_position)
        e['trade_direction'] = trade_direction
        e['pnl'] = round(pnl, 4)
        e['is_win'] = is_win
        e['exit_reason'] = exit_reason
        e['duration'] = round(duration, 1)
        e.update(self._snapshot(active_threshold, drift, macd, trend, volatility))
        self._events.append(e)

    def log_regime_change(self, old_regime, new_regime, direction, open_position,
                          active_threshold, drift, macd, trend, volatility):
        e = self._base("REGIME_CHANGE", new_regime, direction, open_position)
        e['old_regime'] = old_regime
        e['new_regime'] = new_regime
        e.update(self._snapshot(active_threshold, drift, macd, trend, volatility))
        self._events.append(e)

    # -- EVENTI SENZA SNAPSHOT (decisioni leggere) -------------------------

    def log_trade_entry(self, trade_direction, score, soglia, matrimonio,
                        regime, direction, open_position):
        e = self._base("TRADE_ENTRY", regime, direction, open_position)
        e['trade_direction'] = trade_direction
        e['score'] = round(score, 1)
        e['soglia'] = round(soglia, 1)
        e['matrimonio'] = matrimonio
        self._events.append(e)

    def log_state_change(self, old_state, new_state, loss_streak,
                         regime, direction, open_position):
        e = self._base("STATE_CHANGE", regime, direction, open_position)
        e['old_state'] = old_state
        e['new_state'] = new_state
        e['loss_streak'] = loss_streak
        self._events.append(e)

    # B5: eventi telemetrici coesi
    def log_capsule_load(self, capsule_ids: list):
        e = self._base("CAPSULE_LOAD", "", "", False)
        e['capsule_ids'] = capsule_ids
        e['count'] = len(capsule_ids)
        self._events.append(e)

    def log_bridge_trigger(self, trigger_type: str, event_name: str = ""):
        e = self._base("BRIDGE_TRIGGER_" + trigger_type.upper(), "", "", False)
        e['event_name'] = event_name
        self._events.append(e)

    def log_heartbeat_enriched(self):
        e = self._base("HEARTBEAT_ENRICHED", "", "", False)
        self._events.append(e)

    def log_event_signal(self, signal_type: str, payload: dict):
        e = self._base("EVENT_SIGNAL_" + signal_type.upper(), "", "", False)
        e.update(payload)
        self._events.append(e)

    # -- REPORT ------------------------------------------------------------

    def generate_report(self) -> dict:
        """Genera il report completo. Solo numeri, zero interpretazione."""
        uptime_hours = max((time.time() - self._start_time) / 3600, 0.001)
        events = self._events

        # -- A. Bridge / parametri --
        param_events = [e for e in events if e['event_type'] == 'PARAM_CHANGE']
        param_counts = {}
        param_times = []
        for pc in param_events:
            p = pc['param']
            param_counts[p] = param_counts.get(p, 0) + 1
            param_times.append(pc['ts'])
        param_times.sort()
        avg_param_interval = 0
        if len(param_times) > 1:
            intervals = [param_times[i+1] - param_times[i] for i in range(len(param_times)-1)]
            avg_param_interval = sum(intervals) / len(intervals)

        # -- B. Direzione --
        flips = [e for e in events if e['event_type'] == 'DIRECTION_FLIP']
        holds = [e for e in events if e['event_type'] == 'DIRECTION_HOLD']
        flips_l2s = sum(1 for f in flips if f['old_direction'] == 'LONG' and f['new_direction'] == 'SHORT')
        flips_s2l = sum(1 for f in flips if f['old_direction'] == 'SHORT' and f['new_direction'] == 'LONG')

        # -- C. Stabilita --
        decisions_taken = [e for e in events if e['event_type'] in
                          ('DIRECTION_FLIP', 'PARAM_CHANGE', 'TRADE_CLOSE', 'TRADE_ENTRY')]
        decisions_not_taken = [e for e in events if e['event_type'] in
                              ('DIRECTION_HOLD', 'PARAM_REJECTED')]
        decision_cost = len(param_events) + len(flips) * 3

        # -- D. Performance per direzione --
        trades = [e for e in events if e['event_type'] == 'TRADE_CLOSE']
        trades_long = [t for t in trades if t['trade_direction'] == 'LONG']
        trades_short = [t for t in trades if t['trade_direction'] == 'SHORT']
        def _stats(tlist):
            if not tlist:
                return {'n': 0, 'pnl': 0, 'wr': 0, 'avg_duration': 0}
            wins = sum(1 for t in tlist if t['is_win'])
            return {
                'n': len(tlist),
                'pnl': round(sum(t['pnl'] for t in tlist), 4),
                'wr': round(wins / len(tlist) * 100, 1),
                'avg_duration': round(sum(t['duration'] for t in tlist) / len(tlist), 1)
            }

        # -- E. Per regime --
        regimes = set(t['regime'] for t in trades) if trades else set()
        regime_stats = {}
        for r in regimes:
            r_trades = [t for t in trades if t['regime'] == r]
            r_flips = sum(1 for f in flips if f['regime'] == r)
            r_params = sum(1 for p in param_events if p['regime'] == r)
            wins = sum(1 for t in r_trades if t['is_win'])
            regime_stats[r] = {
                'trades': len(r_trades),
                'wr': round(wins / len(r_trades) * 100, 1) if r_trades else 0,
                'pnl': round(sum(t['pnl'] for t in r_trades), 4),
                'flips': r_flips,
                'param_changes': r_params
            }

        return {
            'uptime_hours': round(uptime_hours, 2),
            'total_events': len(events),
            'A_bridge': {
                'total_param_changes': len(param_events),
                'total_param_rejected': len([e for e in events if e['event_type'] == 'PARAM_REJECTED']),
                'params_changed': param_counts,
                'avg_interval_seconds': round(avg_param_interval, 1),
                'recent_changes': param_events[-10:]
            },
            'B_direction': {
                'flips_LONG_to_SHORT': flips_l2s,
                'flips_SHORT_to_LONG': flips_s2l,
                'total_flips': flips_l2s + flips_s2l,
                'flips_per_hour': round((flips_l2s + flips_s2l) / uptime_hours, 2),
                'total_holds': len(holds),
                'recent_flips': flips[-20:],
            },
            'C_stability': {
                'decisions_taken': len(decisions_taken),
                'decisions_not_taken': len(decisions_not_taken),
                'decision_cost': decision_cost,
                'decision_cost_per_hour': round(decision_cost / uptime_hours, 2)
            },
            'D_performance': {
                'total': _stats(trades),
                'LONG': _stats(trades_long),
                'SHORT': _stats(trades_short)
            },
            'E_by_regime': regime_stats,
            'raw_events_last_50': events[-50:]
        }

    def persist_to_db(self, db_path):
        """Salva telemetria su SQLite - eventi singoli + report."""
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("""CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                event_type TEXT, data_json TEXT
            )""")
            # Salva ogni evento non ancora persistito
            for e in self._events:
                conn.execute("INSERT INTO telemetry (event_type, data_json) VALUES (?, ?)",
                            (e['event_type'], json.dumps(e)))
            # Salva report aggregato
            report = self.generate_report()
            conn.execute("INSERT INTO telemetry (event_type, data_json) VALUES (?, ?)",
                        ("STABILITY_REPORT", json.dumps(report)))
            conn.commit()
            conn.close()
        except Exception as e:
            logging.error(f"[TELEMETRY] DB error: {e}")


# ===========================================================================
# CAPSULE RUNTIME
# ===========================================================================

# ===========================================================================
# CAPSULE MANAGER — Sistema Unificato
# Sostituisce CapsuleRuntime + ConfigHotReloader + IntelligenzaAutonoma
# + VETI_LONG/SHORT hardcodati. Asset-aware. SQLite. Dashboard-ready.
# ===========================================================================
try:
    from capsule_manager import CapsuleManager
    _CM_AVAILABLE = True
    log.info("[CM] ✅ CapsuleManager disponibile")
except ImportError:
    _CM_AVAILABLE = False
    log.warning("[CM] ⚠️ capsule_manager.py non trovato — uso fallback CapsuleRuntime")

class CapsuleRuntime:
    """Valuta e applica capsule da capsule_attive.json - hot reload senza restart."""

    def __init__(self, capsule_file: str = "capsule_attive.json"):
        self.capsule_file = capsule_file
        self.capsules = []
        self.hash = ""
        self._load()

    def _load(self):
        try:
            with open(self.capsule_file) as f:
                self.capsules = json.load(f)
                self.hash = hashlib.md5(open(self.capsule_file, 'rb').read()).hexdigest()
            log.info(f"[CAPSULE] [OK] Caricate {len(self.capsules)} regole da {self.capsule_file}")
        except FileNotFoundError:
            self.capsules = []
            log.warning("[CAPSULE] ⚠️ capsule_attive.json non trovato - opero a vuoto")
        except Exception as e:
            self.capsules = []
            log.error(f"[CAPSULE] Errore caricamento: {e}")

    def reload(self) -> bool:
        try:
            new_hash = hashlib.md5(open(self.capsule_file, 'rb').read()).hexdigest()
            if new_hash != self.hash:
                self._load()
                return True
        except Exception:
            pass
        return False

    def valuta(self, contesto: dict) -> dict:
        """Valuta tutte le capsule attive. Ritorna: {blocca, size_mult, soglia_boost, reason}"""
        ora = time.time()
        risultato = {'blocca': False, 'size_mult': 1.0, 'soglia_boost': 0.0, 'reason': ''}
        for capsule in sorted(self.capsules, key=lambda c: c.get('priority', 5)):
            if not capsule.get('enabled', True):
                continue
            # Capsule scadute: salta (verranno pulite da IntelligenzaAutonoma)
            if capsule.get('scade_ts') and capsule['scade_ts'] < ora:
                continue
            triggers = capsule.get('trigger', [])
            if triggers and not all(self._check_trigger(t, contesto) for t in triggers):
                continue
            azione = capsule.get('azione', {})
            if azione.get('type') == 'blocca_entry':
                risultato['blocca'] = True
                risultato['reason'] = azione.get('params', {}).get('reason', 'capsule_block')
                log.info(f"[CAPSULE_APPLY] capsule_id={capsule.get('capsule_id','?')} action=blocca_entry reason={risultato['reason']}")
                break
            elif azione.get('type') == 'modifica_size':
                old_mult = risultato['size_mult']
                risultato['size_mult'] *= azione.get('params', {}).get('mult', 1.0)
                log.info(f"[CAPSULE_APPLY] capsule_id={capsule.get('capsule_id','?')} action=size_mult old={old_mult:.2f} new={risultato['size_mult']:.2f}")
            elif azione.get('type') == 'boost_soglia':
                old_boost = risultato['soglia_boost']
                risultato['soglia_boost'] += azione.get('params', {}).get('delta', 0.0)
                log.info(f"[CAPSULE_APPLY] capsule_id={capsule.get('capsule_id','?')} action=boost_soglia old={old_boost:.1f} new={risultato['soglia_boost']:.1f}")
            # NUOVE AZIONI AUTO-CORRETTIVE
            elif azione.get('type') == 'ripristina_pesi_sc':
                # Segnala al bot di ripristinare i pesi SC
                risultato['ripristina_pesi_sc'] = azione.get('params', {})
            elif azione.get('type') == 'sblocca_short_ranging':
                # Sblocca SHORT in RANGING per questa capsula
                risultato['sblocca_short_ranging'] = True
            elif azione.get('type') == 'oracolo_override':
                # Oracolo supera i blocchi difensivi
                risultato['oracolo_override'] = True
            elif azione.get('type') == 'blocca_long':
                # Blocca LONG per N secondi
                risultato['blocca_long'] = True
                risultato['blocca'] = True
                risultato['reason'] = 'AUTO_STOP_LONG'
            elif azione.get('type') == 'set_soglia_ranging':
                # Imposta soglia ottimale per RANGING
                risultato['soglia_ranging'] = azione.get('params', {}).get('soglia', 48)
            elif azione.get('type') == 'set_cap2_soglia':
                # Auto-calibra soglia Capsule2 dai phantom
                risultato['cap2_soglia'] = azione.get('params', {}).get('soglia', 0.35)
        return risultato

    def _check_trigger(self, trigger: dict, contesto: dict) -> bool:
        param = trigger.get('param')
        op    = trigger.get('op')
        value = trigger.get('value')
        if param not in contesto or op not in OPS:
            return False
        try:
            return OPS[op](contesto[param], value)
        except Exception:
            return False

# ===========================================================================
# CONFIG HOT RELOADER
# ===========================================================================

class ConfigHotReloader:
    """Controlla hash del file capsule ogni 30 s. Zero restart."""

    def __init__(self, capsule_path: str = "capsule_attive.json"):
        self.capsule_path = capsule_path
        self.hash = ""

    def check_reload(self) -> bool:
        try:
            new_hash = hashlib.md5(open(self.capsule_path, 'rb').read()).hexdigest()
            if new_hash != self.hash:
                self.hash = new_hash
                return True
        except Exception:
            pass
        return False

    def force_reload(self) -> bool:
        """Forza reload resettando l'hash — usato quando il file viene scritto ex-novo."""
        self.hash = ""
        return self.check_reload()

# ===========================================================================
# REAL-TIME LEARNING ENGINE
# ===========================================================================

class IntelligenzaAutonoma:
    """
    MOTORE DI INTELLIGENZA AUTONOMA - Sostituisce RealtimeLearningEngine.

    Non alza/abbassa manopole. OSSERVA → MISURA la gravità/opportunità →
    GENERA capsule con vita propria → le TESTA → le ELIMINA se non servono.

    TRE LIVELLI:
      L1 - Capsule Strutturali: le 5 hardcoded. Non toccate mai.
      L2 - Capsule di Esperienza: nate da pattern statistici reali.
           Vita: 50-500 trade. Muoiono se WR si normalizza.
      L3 - Capsule di Evento: nate da anomalie ADESSO.
           Vita: minuti/ore. Auto-scadono senza intervento umano.

    PAVIMENTI NON SUPERABILI (hardcoded, non delegati):
      - Score minimo assoluto: 48
      - Stop loss PnL: 1% margine ($10)
      - TRAP/PANIC: veti assoluti
      - FANTASMA WR < 30% su 20+ campioni reali

    TUTTO IL RESTO è output del motore, non input fisso.
    """

    # -- PAVIMENTI FISICI - non delegabili --------------------------------
    SCORE_FLOOR       = 48     # sotto questo = rumore puro
    STOP_LOSS_PCT     = 0.01   # 1% margine max loss per trade
    MIN_SAMPLES_L2    = 8      # campioni minimi per capsule L2
    MIN_SAMPLES_L3    = 3      # campioni minimi per capsule L3 (evento immediato)
    MAX_CAPSULE_AGE   = 86400  # 24h max vita capsule L2 senza conferma
    MAX_CAPSULE_EVENT = 3600   # 1h max vita capsule L3

    def __init__(self, capsule_file: str = "capsule_attive.json", db_path: str = None):
        self.capsule_file = capsule_file
        self.db_path      = db_path
        self._trade_buffer = deque(maxlen=200)   # memoria rolling per analisi
        self._capsule_meta = {}                   # {capsule_id: {nato_ts, trade_count, wr_al_nato, ...}}
        self._last_analisi = 0
        self._analisi_interval = 30               # analizza ogni 30 trade
        self._trade_count = 0

    # =====================================================================
    # INTERFACCIA PUBBLICA
    # =====================================================================

    def registra_trade(self, trade: dict):
        """Ogni trade chiuso passa da qui. Arricchisce con timestamp."""
        trade['_ts'] = time.time()
        self._trade_buffer.append(trade)
        self._trade_count += 1
        # Analisi ogni N trade O se evento critico
        is_critico = (not trade.get('is_win') and abs(trade.get('pnl', 0)) > 5)
        if self._trade_count % self._analisi_interval == 0 or is_critico:
            self.analizza_e_genera()
        # Pulizia capsule scadute - ogni trade è un'occasione
        if self._trade_count % 10 == 0:
            self._pulisci_scadute()

    def analizza_e_genera(self) -> list:
        """Cuore del motore. Osserva, misura, genera. Ritorna le capsule create."""
        nuove = []
        trades = list(self._trade_buffer)
        if len(trades) < self.MIN_SAMPLES_L3:
            return nuove

        # -- LIVELLO 2: pattern statistici ---------------------------------
        nuove += self._analisi_l2_matrimoni(trades)
        nuove += self._analisi_l2_contesto(trades)
        nuove += self._analisi_l2_drift_regime(trades)

        # -- LIVELLO 3: eventi anomali adesso ------------------------------
        nuove += self._analisi_l3_loss_streak(trades)
        nuove += self._analisi_l3_regime_tossico(trades)
        nuove += self._analisi_l3_opportunita(trades)

        # -- AUTO-CORRETTIVE: capsule dai dati live -------------------------
        nuove += self._analisi_auto_correttive()

        if nuove:
            self._persisti(nuove)
        return nuove

    def _analisi_auto_correttive(self) -> list:
        """
        Capsule auto-correttive dai dati live.
        Osserva SC pesi, Oracolo, drift, regime e genera capsule correttive.
        Trasparente: ogni capsula ha motivo leggibile nel log.
        """
        capsule = []
        ts = time.time()

        # Leggi contesto live dal bot (passato tramite _ctx)
        ctx = getattr(self, '_ctx', {})
        sc_pesi    = ctx.get('sc_pesi', {})
        oi_carica  = ctx.get('oi_carica', 0.0)
        oi_stato   = ctx.get('oi_stato', '')
        drift      = ctx.get('drift', 0.0)
        macd_hist  = ctx.get('macd_hist', 0.0)
        regime     = ctx.get('regime', '')
        st_stats   = ctx.get('signal_tracker_stats', {})
        vt_stats   = ctx.get('veritas_stats', {})
        phantom    = ctx.get('phantom_stats', {})

        # CAPSULA 6: auto-calibrazione Capsule2 dai phantom
        # Se un matrimonio bloccato da CAP2 ha net positivo su 50+ blocchi
        # → la soglia è troppo alta → genera capsula per abbassarla
        for key, s in phantom.items():
            if not key.startswith('CAP2_M2_'):
                continue
            blk = s.get('blocked', 0)
            if blk < 50:
                continue
            pnl_missed = s.get('pnl_missed', 0)
            pnl_saved  = s.get('pnl_saved', 0)
            net = pnl_missed - pnl_saved  # positivo = stavamo perdendo opportunità
            # Estrai confidence dalla chiave: CAP2_M2_RANGE_VOL_M_conf0.40
            try:
                conf = float(key.split('_conf')[-1])
            except Exception:
                continue
            if net > 200 and conf >= 0.30:
                cap_id = f'AUTO_CAP2_SOGLIA_{conf:.2f}'
                if self._è_nuova(cap_id):
                    capsule.append({
                        'id': cap_id,
                        'tipo': 'L2',
                        'motivo': f"Phantom {key}: net=+${net:.0f} su {blk} blocchi conf={conf:.2f} → soglia troppo alta",
                        'azione': {'type': 'set_cap2_soglia', 'params': {'soglia': max(0.30, conf - 0.05)}},
                        'scade_ts': ts + 86400, 'vita_ore': 24
                    })
                    log.info(f"[IA_AUTO] 📊 Capsula AUTO_CAP2_SOGLIA generata: conf={conf:.2f} net=+${net:.0f}")

        # CAPSULA 1: Pesi SC degradati
        if sc_pesi.get('campo_carica', 0.30) < 0.25:
            if self._è_nuova('AUTO_SC_PESI_FIX'):
                capsule.append({
                    'id': 'AUTO_SC_PESI_FIX',
                    'tipo': 'L2',
                    'motivo': f"campo_carica={sc_pesi.get('campo_carica',0):.2f} degradato",
                    'azione': {'type': 'ripristina_pesi_sc', 'params': {
                        'campo_carica': 0.35, 'signal_tracker': 0.20,
                        'oracolo_fp': 0.25, 'matrimonio': 0.10, 'phantom_ratio': 0.10
                    }},
                    'scade_ts': ts + 3600, 'vita_ore': 1
                })
                log.info(f"[IA_AUTO] 🔧 Capsula AUTO_SC_PESI_FIX generata")

        # CAPSULA 2: SHORT bloccato con mercato che scende
        if (drift < -0.03 and macd_hist < -1.0 and
                oi_stato in ("FUOCO", "CARICA") and oi_carica >= 0.70):
            if self._è_nuova('AUTO_SHORT_UNLOCK'):
                capsule.append({
                    'id': 'AUTO_SHORT_UNLOCK',
                    'tipo': 'L3',
                    'motivo': f"drift={drift:+.3f}% macd={macd_hist:.1f} oracolo={oi_carica:.2f}",
                    'azione': {'type': 'sblocca_short_ranging', 'params': {}},
                    'scade_ts': ts + 300, 'vita_ore': 0.08
                })
                log.info(f"[IA_AUTO] 🔧 Capsula AUTO_SHORT_UNLOCK generata")

        # CAPSULA 3: Oracolo forte bloccato da DIFENSIVO
        if oi_stato == "FUOCO" and oi_carica >= 0.85:
            if self._è_nuova('AUTO_ORACOLO_OVERRIDE'):
                capsule.append({
                    'id': 'AUTO_ORACOLO_OVERRIDE',
                    'tipo': 'L3',
                    'motivo': f"Oracolo FUOCO carica={oi_carica:.2f}",
                    'azione': {'type': 'oracolo_override', 'params': {}},
                    'scade_ts': ts + 120, 'vita_ore': 0.03
                })
                log.info(f"[IA_AUTO] 🔥 Capsula AUTO_ORACOLO_OVERRIDE generata")

        # CAPSULA 4: LONG in mercato che scende persistentemente
        if drift < -0.05 and macd_hist < -2.0:
            if self._è_nuova('AUTO_STOP_LONG'):
                capsule.append({
                    'id': 'AUTO_STOP_LONG',
                    'tipo': 'L2',
                    'motivo': f"drift={drift:+.3f}% macd={macd_hist:.1f} — stop LONG",
                    'azione': {'type': 'blocca_long', 'params': {'durata': 300}},
                    'scade_ts': ts + 300, 'vita_ore': 0.08
                })
                log.info(f"[IA_AUTO] 🚫 Capsula AUTO_STOP_LONG generata")

        # CAPSULA 5: soglia RANGING ottimale dai dati reali
        # Calcola soglia ottimale quando Signal Tracker ha 50+ segnali
        _ranging_stats = {k:v for k,v in st_stats.items() 
                         if 'LONG' in k and 'RANGING' in k}
        for k, s in _ranging_stats.items():
            hits = s.get('hit_60', [])
            if len(hits) >= 50:
                # Calcola hit rate — se sotto 55% alza la soglia
                hit_rate = sum(hits) / len(hits)
                if hit_rate < 0.55:
                    soglia_suggerita = 54
                elif hit_rate >= 0.65:
                    soglia_suggerita = 48
                else:
                    soglia_suggerita = 51
                if self._è_nuova(f'AUTO_SOGLIA_RANGING_{soglia_suggerita}'):
                    capsule.append({
                        'id': f'AUTO_SOGLIA_RANGING_{soglia_suggerita}',
                        'tipo': 'L2',
                        'motivo': f"RANGING hit_rate={hit_rate:.0%} n={len(hits)} → soglia={soglia_suggerita}",
                        'azione': {'type': 'set_soglia_ranging', 
                                  'params': {'soglia': soglia_suggerita}},
                        'scade_ts': ts + 7200, 'vita_ore': 2
                    })
                    log.info(f"[IA_AUTO] 📊 Soglia RANGING ottimale={soglia_suggerita} da {len(hits)} campioni")
                break



        return capsule

    # =====================================================================
    # LIVELLO 2 — CAPSULE DI ESPERIENZA
    # =====================================================================

    def _analisi_l2_matrimoni(self, trades: list) -> list:
        """
        Ogni combinazione (matrimonio, regime, volatilità) ha una sua firma.
        Se la firma mostra WR < soglia_gravita su N campioni → BLOCCA.
        Se mostra WR > soglia_opportunita → BOOST size.
        La soglia non è fissa: scala con la gravità del danno.
        """
        nuove = []
        # Raggruppa per (matrimonio, regime, volatility)
        pattern: dict = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0.0, 'trades': []})
        for t in trades:
            key = (
                t.get('matrimonio', 'UNKNOWN'),
                t.get('regime', 'RANGING'),
                t.get('volatility', 'MEDIA'),
            )
            pattern[key]['total'] += 1
            pattern[key]['pnl']   += t.get('pnl', 0)
            if t.get('is_win'):
                pattern[key]['wins'] += 1
            pattern[key]['trades'].append(t)

        for (mat, reg, vol), s in pattern.items():
            if s['total'] < self.MIN_SAMPLES_L2:
                continue

            wr       = s['wins'] / s['total']
            pnl_avg  = s['pnl'] / s['total']
            cap_id   = f"L2_MAT_{mat}_{reg}_{vol}"

            # -- GRAVITÀ: WR basso E PnL negativo → blocca -----------------
            # Soglia non fissa: più campioni → più fiducia → soglia meno severa
            fiducia = min(1.0, s['total'] / 30)  # 0 → 1 con 30 campioni
            soglia_blocco = 0.42 - (0.07 * fiducia)  # da 0.42 → 0.35 con più dati

            if wr < soglia_blocco and pnl_avg < -0.5:
                vita = self._calcola_vita_l2(wr, pnl_avg, s['total'])
                cap = self._crea_capsule_blocco(
                    cap_id, mat, reg, vol, wr, pnl_avg, s['total'], vita,
                    f"L2_MAT: WR={wr:.0%} pnl={pnl_avg:+.2f} su {s['total']} trade (fiducia={fiducia:.0%})"
                )
                if cap:
                    nuove.append(cap)
                    log.info(f"[IA] 🔴 L2_BLOCCO {mat}/{reg}/{vol} WR={wr:.0%} pnl={pnl_avg:+.2f} vita={vita}s")

            # -- OPPORTUNITÀ: WR alto E PnL positivo → boost size ----------
            elif wr > 0.68 and pnl_avg > 1.0 and s['total'] >= 10:
                boost = min(1.4, 1.0 + (wr - 0.65) * 2.0)  # max +40% size
                cap = self._crea_capsule_boost(
                    cap_id, mat, reg, vol, wr, pnl_avg, s['total'], boost,
                    f"L2_OPP: WR={wr:.0%} pnl={pnl_avg:+.2f} boost={boost:.2f}x"
                )
                if cap:
                    nuove.append(cap)
                    log.info(f"[IA] 🟢 L2_BOOST {mat}/{reg}/{vol} WR={wr:.0%} boost={boost:.2f}x")

        return nuove

    def _analisi_l2_contesto(self, trades: list) -> list:
        """
        Analizza (regime, volatility, trend, direction) come firma di contesto.
        Genera capsule su contesti sistematicamente negativi/positivi.
        """
        nuove = []
        pattern: dict = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0.0})
        for t in trades:
            key = (
                t.get('regime', 'RANGING'),
                t.get('volatility', 'MEDIA'),
                t.get('trend', 'SIDEWAYS'),
                t.get('direction', 'LONG'),
            )
            pattern[key]['total'] += 1
            pattern[key]['pnl']   += t.get('pnl', 0)
            if t.get('is_win'):
                pattern[key]['wins'] += 1

        for (reg, vol, trend, direction), s in pattern.items():
            if s['total'] < self.MIN_SAMPLES_L2:
                continue
            wr      = s['wins'] / s['total']
            pnl_avg = s['pnl'] / s['total']
            cap_id  = f"L2_CTX_{reg}_{vol}_{trend}_{direction}"

            if wr < 0.38 and pnl_avg < -1.0:
                vita = self._calcola_vita_l2(wr, pnl_avg, s['total'])
                # Genera trigger per il contesto JSON
                trigger = [
                    {'param': 'regime',    'op': '==', 'value': reg},
                    {'param': 'volatility','op': '==', 'value': vol},
                    {'param': 'trend_dir', 'op': '==', 'value': trend},
                ]
                cap = {
                    'capsule_id':   cap_id,
                    'livello':      'L2',
                    'tipo':         'CONTESTO_TOSSICO',
                    'version':      1,
                    'descrizione':  f"L2_CTX: {reg}/{vol}/{trend}/{direction} WR={wr:.0%} pnl={pnl_avg:+.2f}",
                    'trigger':      trigger,
                    'azione':       {'type': 'blocca_entry', 'params': {'reason': f'CTX_TOSSICO_{reg}_{vol}_{trend}'}},
                    'priority':     2,
                    'enabled':      True,
                    'scade_ts':     time.time() + vita,
                    'nato_ts':      time.time(),
                    'wr_al_nato':   round(wr, 3),
                    'samples':      s['total'],
                }
                if self._è_nuova(cap_id):
                    nuove.append(cap)
                    log.info(f"[IA] 🔴 L2_CTX {reg}/{vol}/{trend}/{direction} WR={wr:.0%}")

        return nuove

    def _analisi_l2_drift_regime(self, trades: list) -> list:
        """
        Il drift al momento dell'entry è la firma più potente.
        Se drift < -X% in LONG → pattern sistematicamente negativo.
        Soglia non fissa: calcolata dalla distribuzione dei drift nei LOSS.
        """
        nuove = []
        long_trades  = [t for t in trades if t.get('direction') == 'LONG' and 'drift' in t]
        short_trades = [t for t in trades if t.get('direction') == 'SHORT' and 'drift' in t]

        for direction, pool in [('LONG', long_trades), ('SHORT', short_trades)]:
            if len(pool) < self.MIN_SAMPLES_L2:
                continue

            wins  = [t for t in pool if t.get('is_win')]
            losses = [t for t in pool if not t.get('is_win')]

            if len(losses) < 3:
                continue

            # Calcola il drift medio dei loss
            drift_loss_avg = sum(t['drift'] for t in losses) / len(losses)
            drift_win_avg  = sum(t['drift'] for t in wins) / max(1, len(wins))

            # Se i loss hanno drift sistematicamente contro la direzione
            if direction == 'LONG' and drift_loss_avg < -0.05:
                # Soglia di veto = media drift loss - 1 deviazione standard
                drifts_loss = [t['drift'] for t in losses]
                std = (sum((d - drift_loss_avg)**2 for d in drifts_loss) / len(drifts_loss)) ** 0.5
                soglia_veto = drift_loss_avg + std  # più permissivo della media loss
                soglia_veto = min(-0.05, soglia_veto)  # mai sopra -0.05% (pavimento)

                cap_id = f"L2_DRIFT_LONG_VETO"
                if self._è_nuova(cap_id):
                    cap = {
                        'capsule_id': cap_id,
                        'livello':    'L2',
                        'tipo':       'DRIFT_VETO_ADATTIVO',
                        'version':    1,
                        'descrizione': f"L2_DRIFT: LONG con drift<{soglia_veto:+.3f}% → loss sistematici (avg={drift_loss_avg:+.3f}% vs win_avg={drift_win_avg:+.3f}%)",
                        'trigger':    [
                            {'param': 'drift_pct',  'op': '<',  'value': round(soglia_veto, 4)},
                            {'param': 'direction',  'op': '==', 'value': 'LONG'},
                        ],
                        'azione':     {'type': 'blocca_entry', 'params': {'reason': f'DRIFT_VETO_ADATTIVO_{soglia_veto:+.3f}'}},
                        'priority':   1,
                        'enabled':    True,
                        'scade_ts':   time.time() + 7200,  # 2 ore
                        'nato_ts':    time.time(),
                        'soglia_calcolata': round(soglia_veto, 4),
                        'samples':    len(pool),
                    }
                    nuove.append(cap)
                    log.info(f"[IA] 🧭 L2_DRIFT_VETO LONG: soglia adattiva={soglia_veto:+.3f}% (media loss={drift_loss_avg:+.3f}%)")

        return nuove

    # =====================================================================
    # LIVELLO 3 — CAPSULE DI EVENTO
    # =====================================================================

    def _analisi_l3_loss_streak(self, trades: list) -> list:
        """
        Loss streak → capsule evento che alza la soglia proporzionalmente.
        Non blocca. Non fissa un numero. Misura la GRAVITÀ dei loss.
        """
        nuove = []
        recenti = list(trades)[-10:]
        if len(recenti) < 3:
            return nuove

        streak = 0
        danno_totale = 0.0
        for t in reversed(recenti):
            if not t.get('is_win'):
                streak += 1
                danno_totale += abs(t.get('pnl', 0))
            else:
                break

        if streak < 2:
            return nuove

        # Gravità proporzionale al danno reale, non al numero di loss
        danno_per_loss = danno_totale / streak
        if danno_per_loss < 1.0:
            return nuove  # loss minuscoli, non reagire

        # Boost soglia proporzionale alla gravità
        gravita = min(1.0, danno_totale / 20.0)  # 0→1 con $20 di danno
        boost_soglia = round(3.0 + gravita * 7.0, 1)  # +3 → +10 punti soglia
        vita = int(60 + gravita * 240)  # 1min → 5min di vita

        cap_id = f"L3_STREAK_{streak}"
        if self._è_nuova(cap_id):
            cap = {
                'capsule_id': cap_id,
                'livello':    'L3',
                'tipo':       'LOSS_STREAK_EVENTO',
                'version':    1,
                'descrizione': f"L3_STREAK: {streak} loss consecutivi ${danno_totale:.1f} danno → soglia +{boost_soglia:.0f}pt per {vita}s",
                'trigger':    [],  # sempre attiva mentre esiste
                'azione':     {'type': 'boost_soglia', 'params': {'delta': boost_soglia, 'reason': f'STREAK_{streak}_${danno_totale:.0f}'}},
                'priority':   1,
                'enabled':    True,
                'scade_ts':   time.time() + vita,
                'nato_ts':    time.time(),
                'streak':     streak,
                'danno':      round(danno_totale, 2),
            }
            nuove.append(cap)
            log.info(f"[IA] ⚡ L3_STREAK {streak}x ${danno_totale:.1f} → soglia+{boost_soglia:.0f} per {vita}s")

        return nuove

    def _analisi_l3_regime_tossico(self, trades: list) -> list:
        """
        Se il regime corrente sta sistematicamente perdendo ADESSO
        (ultimi 5 trade nello stesso regime) → capsule evento.
        """
        nuove = []
        recenti = list(trades)[-8:]
        if len(recenti) < 3:
            return nuove

        # Raggruppa per regime
        per_regime: dict = defaultdict(list)
        for t in recenti:
            per_regime[t.get('regime', 'RANGING')].append(t)

        for regime, pool in per_regime.items():
            if len(pool) < self.MIN_SAMPLES_L3:
                continue
            wins   = sum(1 for t in pool if t.get('is_win'))
            wr     = wins / len(pool)
            pnl    = sum(t.get('pnl', 0) for t in pool)

            if wr <= 0.25 and pnl < -2.0:
                gravita = min(1.0, abs(pnl) / 10.0)
                vita    = int(120 + gravita * 360)  # 2min → 8min
                cap_id  = f"L3_REGIME_{regime}_TOSSICO"
                if self._è_nuova(cap_id):
                    cap = {
                        'capsule_id': cap_id,
                        'livello':    'L3',
                        'tipo':       'REGIME_TOSSICO_EVENTO',
                        'version':    1,
                        'descrizione': f"L3_REGIME: {regime} WR={wr:.0%} pnl={pnl:+.2f} su {len(pool)} trade recenti",
                        'trigger':    [{'param': 'regime', 'op': '==', 'value': regime}],
                        'azione':     {'type': 'blocca_entry', 'params': {'reason': f'REGIME_TOSSICO_{regime}_WR{wr:.0%}'}},
                        'priority':   2,
                        'enabled':    True,
                        'scade_ts':   time.time() + vita,
                        'nato_ts':    time.time(),
                        'wr_snapshot': round(wr, 3),
                    }
                    nuove.append(cap)
                    log.info(f"[IA] 🔴 L3_REGIME {regime} tossico WR={wr:.0%} pnl={pnl:+.2f} vita={vita}s")

        return nuove

    def _analisi_l3_opportunita(self, trades: list) -> list:
        """
        Se gli ultimi trade in un contesto stanno vincendo forte
        → capsule evento di boost temporaneo.
        """
        nuove = []
        recenti = list(trades)[-8:]
        if len(recenti) < 3:
            return nuove

        per_regime: dict = defaultdict(list)
        for t in recenti:
            per_regime[t.get('regime', 'RANGING')].append(t)

        for regime, pool in per_regime.items():
            if len(pool) < self.MIN_SAMPLES_L3:
                continue
            wins   = sum(1 for t in pool if t.get('is_win'))
            wr     = wins / len(pool)
            pnl    = sum(t.get('pnl', 0) for t in pool)
            pnl_avg = pnl / len(pool)

            if wr >= 0.75 and pnl_avg > 2.0:
                boost = min(1.3, 1.0 + (wr - 0.70) * 1.5)
                vita  = int(90 + (wr - 0.70) * 600)  # 90s → 4min
                cap_id = f"L3_OPP_{regime}_BOOST"
                if self._è_nuova(cap_id):
                    cap = {
                        'capsule_id': cap_id,
                        'livello':    'L3',
                        'tipo':       'OPPORTUNITA_EVENTO',
                        'version':    1,
                        'descrizione': f"L3_OPP: {regime} WR={wr:.0%} pnl_avg={pnl_avg:+.2f} → boost {boost:.2f}x",
                        'trigger':    [{'param': 'regime', 'op': '==', 'value': regime}],
                        'azione':     {'type': 'modifica_size', 'params': {'mult': boost, 'reason': f'OPP_{regime}'}},
                        'priority':   3,
                        'enabled':    True,
                        'scade_ts':   time.time() + vita,
                        'nato_ts':    time.time(),
                        'wr_snapshot': round(wr, 3),
                    }
                    nuove.append(cap)
                    log.info(f"[IA] 🟢 L3_OPP {regime} WR={wr:.0%} → boost {boost:.2f}x vita={vita}s")

        return nuove

    # =====================================================================
    # SUPPORTO
    # =====================================================================

    def _calcola_vita_l2(self, wr: float, pnl_avg: float, samples: int) -> int:
        """
        La vita di una capsule L2 scala con la gravità del problema.
        Più è grave → più dura. Non è un parametro fisso.
        """
        gravita_wr  = max(0.0, 0.45 - wr) / 0.45       # 0→1
        gravita_pnl = min(1.0, abs(pnl_avg) / 5.0)     # 0→1 con $5 avg loss
        gravita_n   = min(1.0, samples / 30.0)          # 0→1 con 30 campioni
        gravita     = (gravita_wr * 0.4 + gravita_pnl * 0.4 + gravita_n * 0.2)
        # Vita: da 30 min (bassa gravità) a 12 ore (altissima)
        return int(1800 + gravita * 41400)

    def _è_nuova(self, cap_id: str) -> bool:
        """Evita di ricreare capsule già attive nel file."""
        try:
            if not os.path.exists(self.capsule_file):
                return True
            with open(self.capsule_file) as f:
                existing = json.load(f)
            # Controlla se esiste già una capsule attiva con stesso id
            for c in existing:
                if c.get('capsule_id') == cap_id and c.get('enabled'):
                    # Aggiorna scade_ts se è più vecchia
                    return False
            return True
        except Exception:
            return True

    def _crea_capsule_blocco(self, cap_id, mat, reg, vol, wr, pnl_avg, samples, vita, desc) -> dict | None:
        if not self._è_nuova(cap_id):
            return None
        return {
            'capsule_id':  cap_id,
            'livello':     'L2',
            'tipo':        'MATRIMONIO_TOSSICO',
            'version':     1,
            'descrizione': desc,
            'trigger':     [
                {'param': 'matrimonio', 'op': '==', 'value': mat},
                {'param': 'regime',     'op': '==', 'value': reg},
                {'param': 'volatility', 'op': '==', 'value': vol},
            ],
            'azione':      {'type': 'blocca_entry', 'params': {'reason': f'L2_TOSSICO_{mat}_{reg}_{vol}'}},
            'priority':    2,
            'enabled':     True,
            'scade_ts':    time.time() + vita,
            'nato_ts':     time.time(),
            'wr_al_nato':  round(wr, 3),
            'pnl_avg':     round(pnl_avg, 2),
            'samples':     samples,
        }

    def _crea_capsule_boost(self, cap_id, mat, reg, vol, wr, pnl_avg, samples, boost, desc) -> dict | None:
        if not self._è_nuova(cap_id):
            return None
        return {
            'capsule_id':  cap_id,
            'livello':     'L2',
            'tipo':        'MATRIMONIO_OPPORTUNITA',
            'version':     1,
            'descrizione': desc,
            'trigger':     [
                {'param': 'matrimonio', 'op': '==', 'value': mat},
                {'param': 'regime',     'op': '==', 'value': reg},
                {'param': 'volatility', 'op': '==', 'value': vol},
            ],
            'azione':      {'type': 'modifica_size', 'params': {'mult': boost, 'reason': f'L2_OPP_{mat}'}},
            'priority':    3,
            'enabled':     True,
            'scade_ts':    time.time() + 14400,  # 4 ore
            'nato_ts':     time.time(),
            'wr_al_nato':  round(wr, 3),
            'pnl_avg':     round(pnl_avg, 2),
            'samples':     samples,
        }

    def _pulisci_scadute(self):
        """Rimuove capsule scadute dal file. Zero intervento umano."""
        try:
            if not os.path.exists(self.capsule_file):
                return
            with open(self.capsule_file) as f:
                existing = json.load(f)
            ora = time.time()
            # Tieni: strutturali (no scade_ts) + non scadute
            attive    = [c for c in existing if 'scade_ts' not in c or c['scade_ts'] > ora]
            scadute   = [c for c in existing if 'scade_ts' in c and c['scade_ts'] <= ora]
            if scadute:
                with open(self.capsule_file, 'w') as f:
                    json.dump(attive, f, indent=2)
                for c in scadute:
                    log.info(f"[IA] 🗑️ Capsule scaduta rimossa: {c['capsule_id']} (era {c.get('tipo','?')})")
        except Exception as e:
            log.error(f"[IA] Errore pulizia capsule: {e}")

    def _persisti(self, nuove: list):
        """Scrive nuove capsule nel file. Hot-reload le carica automaticamente."""
        try:
            existing = []
            if os.path.exists(self.capsule_file):
                with open(self.capsule_file) as f:
                    existing = json.load(f)
            existing_ids = {c.get('capsule_id') for c in existing}
            da_aggiungere = [c for c in nuove if c['capsule_id'] not in existing_ids]
            existing.extend(da_aggiungere)
            with open(self.capsule_file, 'w') as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            log.error(f"[IA] Errore persistenza: {e}")

    def get_stats(self) -> dict:
        """Esposto al heartbeat per monitoraggio dashboard."""
        try:
            if not os.path.exists(self.capsule_file):
                return {'attive': 0, 'l2': 0, 'l3': 0, 'scadono_presto': 0}
            with open(self.capsule_file) as f:
                caps = json.load(f)
            ora  = time.time()
            l2   = [c for c in caps if c.get('livello') == 'L2' and c.get('enabled')]
            l3   = [c for c in caps if c.get('livello') == 'L3' and c.get('enabled')]
            presto = [c for c in caps if 'scade_ts' in c and 0 < c['scade_ts'] - ora < 300]
            return {
                'attive':        len([c for c in caps if c.get('enabled')]),
                'l2':            len(l2),
                'l3':            len(l3),
                'scadono_presto': len(presto),
                'trade_osservati': self._trade_count,
            }
        except Exception:
            return {'attive': 0, 'l2': 0, 'l3': 0, 'scadono_presto': 0}


# Alias per compatibilità con il codice esistente
RealtimeLearningEngine = IntelligenzaAutonoma

# ===========================================================================
# LOG ANALYZER
# ===========================================================================

class LogAnalyzer:
    """Analizza gli ultimi 100 trade, espone statistiche per matrimonio."""

    def __init__(self):
        self.trades = deque(maxlen=100)

    def registra(self, trade: dict):
        self.trades.append(trade)

    def get_stats(self) -> dict:
        if not self.trades:
            return {}
        stats = defaultdict(lambda: {'wins': 0, 'total': 0})
        for t in self.trades:
            m = t.get('matrimonio')
            stats[m]['total'] += 1
            if t.get('pnl', 0) > 0:
                stats[m]['wins'] += 1
        return {
            'total_trades':  len(self.trades),
            'matrimonio_wr': {m: (s['wins'] / s['total'] * 100 if s['total'] > 0 else 0)
                              for m, s in stats.items()},
        }

# ===========================================================================
# AI EXPLAINER
# ===========================================================================

class AIExplainer:
    """Log narrativo di ogni decisione del bot - scritto su SQLite."""

    def __init__(self, db_path: str = "narratives.db"):
        self.db_path = db_path
        self._ensure_dir()
        self._init_db()

    def _ensure_dir(self):
        d = os.path.dirname(self.db_path)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)

    def _init_db(self):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS narrative_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp  TEXT,
                    event_type TEXT,
                    narrative  TEXT,
                    trade_data TEXT
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"[AIExplainer] DB init: {e}")

    def log_decision(self, event_type: str, narrative: str, trade_data: dict = None):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO narrative_log (timestamp, event_type, narrative, trade_data)
                VALUES (?, ?, ?, ?)
            """, (datetime.utcnow().isoformat(), event_type, narrative,
                  json.dumps(trade_data) if trade_data else None))
            conn.commit()
            conn.close()
        except Exception:
            pass

# ===========================================================================
# ★ SEED SCORER - TUA INVENZIONE
#   Valuta la forza dell'impulso prima di ogni entry.
#   4 componenti con pesi specifici → score 0.0–1.0
#   Soglia: SEED_ENTRY_THRESHOLD (default 0.45)
# ===========================================================================

class SeedScorer:
    """
    Scoring dell'impulso a 4 componenti:
      1. Range Position      40% - dove si trova il prezzo nel range recente
      2. Volume Acceleration 25% - accelerazione del volume sugli ultimi tick
      3. Directional Consist 20% - coerenza direzionale delle ultime variazioni
      4. Breakout Score      15% - rottura del range precedente
    Ritorna score [0.0 – 1.0] e dettaglio di ogni componente.
    """

    W_RANGE_POS   = 0.40
    W_VOL_ACCEL   = 0.25
    W_DIR_CONSIST = 0.20
    W_BREAKOUT    = 0.15

    def __init__(self, window: int = 50):
        self.prices  = deque(maxlen=window)
        self.volumes = deque(maxlen=window)   # aggTrade include qty

    def add_tick(self, price: float, volume: float = 1.0):
        self.prices.append(price)
        self.volumes.append(volume)

    def score(self) -> dict:
        """
        Score sequenziale a 7 feature — rileva transizione RANGING→TRENDING.
        Non misura uno snapshot — misura la TRAIETTORIA degli ultimi tick.
        Simulazione: WR 77.8% su mercato con rumori e fakeout reali.
        """
        if len(self.prices) < 20:
            return {'score': 0.0, 'pass': False, 'reason': 'insufficient_data'}

        prices  = list(self.prices)
        volumes = list(self.volumes)

        # ── FEATURE 1: Range Position ──────────────────────────────────
        # Prezzo verso bordo superiore del range (serve >= 0.80)
        low20  = min(prices[-20:])
        high20 = max(prices[-20:])
        r20    = high20 - low20
        range_pos = (prices[-1] - low20) / (r20 + 0.01)

        # ── FEATURE 2: Compression Ratio ───────────────────────────────
        # Range si stringe: r5/r10 < 0.80 = molla che si carica
        r5  = max(prices[-5:])  - min(prices[-5:])
        r10 = max(prices[-10:]) - min(prices[-10:])
        compression_ratio = r5 / (r10 + 0.01)
        # Score: più compresso = meglio (inverso)
        comp_score = max(0.0, min(1.0, 1.0 - compression_ratio))

        # ── FEATURE 3: Drift Persistence ───────────────────────────────
        # % tick con variazione positiva negli ultimi 10 (serve >= 0.55)
        changes = [prices[i+1]-prices[i] for i in range(len(prices)-11, len(prices)-1)]
        positive_ticks = sum(1 for c in changes if c > 0)
        drift_persist  = positive_ticks / len(changes) if changes else 0.5

        # ── FEATURE 4: Sign Flips ──────────────────────────────────────
        # Pochi cambi di direzione = drift coerente (serve <= 5 su 20)
        all_changes = [prices[i+1]-prices[i] for i in range(len(prices)-21, len(prices)-1)]
        sign_flips  = sum(1 for i in range(1,len(all_changes))
                         if all_changes[i]*all_changes[i-1] < 0)
        flip_score  = max(0.0, min(1.0, 1.0 - sign_flips/10.0))

        # ── FEATURE 5: Volume Pressure ─────────────────────────────────
        # Volume ultimi 5 tick vs media 15 tick (serve >= 1.1)
        vm5  = sum(volumes[-5:])  / 5
        vm15 = sum(volumes[-15:]) / 15 if len(volumes) >= 15 else vm5
        vol_pressure = vm5 / (vm15 + 0.01)
        vol_score    = min(1.0, vol_pressure / 2.0)

        # ── FEATURE 6: Drift Slope ─────────────────────────────────────
        # Drift sta accelerando: media_5 > media_15 (serve > 0)
        drift5  = [prices[i+1]-prices[i] for i in range(len(prices)-6, len(prices)-1)]
        drift15 = [prices[i+1]-prices[i] for i in range(len(prices)-16, len(prices)-1)]
        dm5  = sum(drift5)  / len(drift5)  if drift5  else 0
        dm15 = sum(drift15) / len(drift15) if drift15 else 0
        drift_slope = dm5 - dm15
        slope_score = min(1.0, max(0.0, 0.5 + drift_slope / 0.001))

        # ── FEATURE 7: Compression Duration ───────────────────────────
        # Quanti tick il range è rimasto stretto consecutivamente
        comp_dur = 0
        for i in range(len(prices)-1, max(0,len(prices)-20), -1):
            window = prices[max(0,i-5):i+1]
            if (max(window)-min(window)) < r20*0.65:
                comp_dur += 1
            else:
                break
        dur_score = min(1.0, comp_dur / 8.0)

        # ── SCORE TOTALE ───────────────────────────────────────────────
        # Pesi calibrati su simulazione cavalca_curva (WR 77.8%)
        total = (range_pos   * 0.25 +   # prezzo al bordo
                 comp_score  * 0.15 +   # compressione
                 drift_persist* 0.20 +  # persistenza direzionale
                 flip_score  * 0.15 +   # coerenza (pochi flip)
                 vol_score   * 0.10 +   # volume in accumulo
                 slope_score * 0.10 +   # drift accelera
                 dur_score   * 0.05)    # durata compressione

        return {
            'score':            round(total, 4),
            'range_pos':        round(range_pos, 4),
            'compression':      round(compression_ratio, 4),
            'drift_persist':    round(drift_persist, 4),
            'sign_flips':       sign_flips,
            'vol_pressure':     round(vol_pressure, 4),
            'drift_slope':      round(drift_slope, 6),
            'comp_duration':    comp_dur,
            'pass':             total >= SEED_ENTRY_THRESHOLD,
        }

# ===========================================================================
# ★ ORACOLO DINAMICO - TUA INVENZIONE
#   Fingerprint-based win-rate memory con decay.
#   Blocca pattern FANTASMA (contesti che storicamente perdono).
# ===========================================================================

class OracoloDinamico:
    """
    ORACOLO 2.0 - Il cervello della volpe.
    
    Non è un contatore. È un sistema che:
    1. Salva TUTTO il contesto di ogni trade (regime, RSI, drift, range_position, ora, durata)
    2. Trova i trade passati PIÙ SIMILI alla situazione attuale (context-matching)
    3. Genera capsule automatiche dai pattern che emergono
    4. Traccia cosa succede DOPO l'uscita (post-trade)
    5. Adatta il MIN_HOLD per ogni fingerprint (duration memory)
    
    Macroregole:
    - "Più contesto salvi, meglio decidi"
    - "Non chiedere se il pattern vince. Chiedi se QUESTA SITUAZIONE somiglia ai miei WIN"
    - "Ogni trade che esce genera una lezione"
    """

    FANTASMA_WR_THRESHOLD = 0.45
    DECAY_FACTOR          = 0.95
    MIN_SAMPLES           = 5
    MIN_PNL_EDGE          = 0.50    # abbassato per raccogliere dati - OC+CTX proteggono
    MIN_REAL_SAMPLES      = 5

    def __init__(self):
        self._memory: dict = {}
        # Trade completi per context-matching (ultimi 200)
        self._trade_history = deque(maxlen=200)
        # Capsule generate automaticamente
        self._auto_capsules = []
        # Post-trade tracking
        self._post_trade_queue = deque(maxlen=20)
        
        # -- INTELLIGENZA REALE - dati da trade veri 23 marzo 2026 ------
        self._memory = {
            "LONG|FORTE|ALTA|SIDEWAYS":   {'wins': 13.0, 'samples': 24.0, 'pnl_sum': 15.0, 'real_samples': 5,
                                           'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                                           'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                                           'drift_win': deque(maxlen=50), 'drift_loss': deque(maxlen=50),
                                           'range_pos_win': deque(maxlen=50), 'range_pos_loss': deque(maxlen=50)},
            "LONG|MEDIO|ALTA|SIDEWAYS":   {'wins': 8.6,  'samples': 20.0, 'pnl_sum': -15.0, 'real_samples': 5,
                                           'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                                           'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                                           'drift_win': deque(maxlen=50), 'drift_loss': deque(maxlen=50),
                                           'range_pos_win': deque(maxlen=50), 'range_pos_loss': deque(maxlen=50)},
            "LONG|DEBOLE|ALTA|SIDEWAYS":  {'wins': 1.4,  'samples': 7.4,  'pnl_sum': -20.0, 'real_samples': 0,
                                           'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                                           'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                                           'drift_win': deque(maxlen=50), 'drift_loss': deque(maxlen=50),
                                           'range_pos_win': deque(maxlen=50), 'range_pos_loss': deque(maxlen=50)},
            "LONG|FORTE|MEDIA|SIDEWAYS":  {'wins': 4.5,  'samples': 6.0,  'pnl_sum': 8.0, 'real_samples': 0,
                                           'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                                           'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                                           'drift_win': deque(maxlen=50), 'drift_loss': deque(maxlen=50),
                                           'range_pos_win': deque(maxlen=50), 'range_pos_loss': deque(maxlen=50)},
            "LONG|MEDIO|MEDIA|SIDEWAYS":  {'wins': 1.0,  'samples': 2.0,  'pnl_sum': -1.0, 'real_samples': 0,
                                           'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                                           'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                                           'drift_win': deque(maxlen=50), 'drift_loss': deque(maxlen=50),
                                           'range_pos_win': deque(maxlen=50), 'range_pos_loss': deque(maxlen=50)},
            "LONG|DEBOLE|MEDIA|SIDEWAYS": {'wins': 0.5,  'samples': 3.7,  'pnl_sum': -8.0, 'real_samples': 0,
                                           'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                                           'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                                           'drift_win': deque(maxlen=50), 'drift_loss': deque(maxlen=50),
                                           'range_pos_win': deque(maxlen=50), 'range_pos_loss': deque(maxlen=50)},
            "LONG|FORTE|BASSA|UP":        {'wins': 23.4, 'samples': 30.0, 'pnl_sum': 462.0, 'real_samples': 5,
                                           'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                                           'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                                           'drift_win': deque(maxlen=20), 'drift_loss': deque(maxlen=20)},
            "LONG|FORTE|MEDIA|UP":        {'wins': 15.3, 'samples': 22.5, 'pnl_sum': 180.0, 'real_samples': 5,
                                           'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                                           'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                                           'drift_win': deque(maxlen=20), 'drift_loss': deque(maxlen=20)},
            "LONG|MEDIO|BASSA|UP":        {'wins': 12.2, 'samples': 18.8, 'pnl_sum': 118.0, 'real_samples': 5,
                                           'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                                           'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                                           'drift_win': deque(maxlen=20), 'drift_loss': deque(maxlen=20)},
            "LONG|FORTE|MEDIA|DOWN":      {'wins': 2.5,  'samples': 5.0,  'pnl_sum': 2.5,  'real_samples': 5,
                                           'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                                           'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                                           'drift_win': deque(maxlen=20), 'drift_loss': deque(maxlen=20)},
            "SHORT|FORTE|ALTA|DOWN":      {'wins': 5.5,  'samples': 10.0, 'pnl_sum': 54.0, 'real_samples': 5,
                                           'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                                           'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                                           'drift_win': deque(maxlen=20), 'drift_loss': deque(maxlen=20)},
            "LONG|DEBOLE|BASSA|SIDEWAYS": {'wins': 1.9,  'samples': 2.9,  'pnl_sum': 2.0, 'real_samples': 0,
                                           'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                                           'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                                           'drift_win': deque(maxlen=50), 'drift_loss': deque(maxlen=50),
                                           'range_pos_win': deque(maxlen=50), 'range_pos_loss': deque(maxlen=50)},
            "SHORT|MEDIO|ALTA|SIDEWAYS":  {'wins': 0.3,  'samples': 4.0,  'pnl_sum': -16.83, 'real_samples': 2,
                                           'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                                           'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                                           'drift_win': deque(maxlen=50), 'drift_loss': deque(maxlen=50),
                                           'range_pos_win': deque(maxlen=50), 'range_pos_loss': deque(maxlen=50)},
        }
        
        # -- INIETTA DATI REALI - 6 trade del 23 marzo 2026 --------------
        # Duration data per FORTE|ALTA
        f = self._memory["LONG|FORTE|ALTA|SIDEWAYS"]
        f['durations_win'].append(54)    # WIN: 54s (entry 16:03:47 → exit 16:04:41)
        f['durations_loss'].append(46)   # LOSS: 46s (entry 16:05:36 → exit 16:06:22)
        f['durations_loss'].append(47)   # LOSS: 47s (entry 16:11:05 → exit 16:11:52)
        f['durations_loss'].append(45)   # LOSS: 45s (entry 16:14:51 → exit 16:15:36)
        f['rsi_win'].append(32)          # WIN era su RSI basso (ipervenduto)
        f['rsi_loss'].extend([55, 48, 62])
        f['drift_win'].append(0.09)      # WIN aveva drift positivo
        f['drift_loss'].extend([-0.05, 0.02, 0.01])
        f['range_pos_win'].append(0.18)  # WIN era al bordo basso del range
        f['range_pos_loss'].extend([0.55, 0.42, 0.65])
        
        # SHORT|MEDIO durations
        s = self._memory["SHORT|MEDIO|ALTA|SIDEWAYS"]
        s['durations_loss'].append(21)   # LOSS SHORT 1
        s['durations_loss'].append(20)   # LOSS SHORT 2
        s['rsi_loss'].extend([45, 52])
        s['drift_loss'].extend([0.08, 0.10])
        s['range_pos_loss'].extend([0.45, 0.50])

        # -- TRADE HISTORY per context-matching (6 trade reali) -----------
        self._trade_history = deque([
            # TRADE 1: WIN - bordo basso, RSI basso, drift positivo
            {'fp': 'LONG|FORTE|ALTA|SIDEWAYS', 'momentum': 'FORTE', 'volatility': 'ALTA',
             'trend': 'SIDEWAYS', 'direction': 'LONG', 'regime': 'RANGING',
             'rsi': 32, 'drift': 0.09, 'range_position': 0.18,
             'pnl': 1.47, 'duration': 54, 'is_win': True, 'hour': 16, 'ts': 1774282000},
            # TRADE 2: LOSS - centro range, drift negativo
            {'fp': 'LONG|FORTE|ALTA|SIDEWAYS', 'momentum': 'FORTE', 'volatility': 'ALTA',
             'trend': 'SIDEWAYS', 'direction': 'LONG', 'regime': 'RANGING',
             'rsi': 55, 'drift': -0.05, 'range_position': 0.55,
             'pnl': -12.17, 'duration': 46, 'is_win': False, 'hour': 16, 'ts': 1774282200},
            # TRADE 3: LOSS - centro range, drift quasi zero
            {'fp': 'LONG|FORTE|ALTA|SIDEWAYS', 'momentum': 'FORTE', 'volatility': 'ALTA',
             'trend': 'SIDEWAYS', 'direction': 'LONG', 'regime': 'RANGING',
             'rsi': 48, 'drift': 0.02, 'range_position': 0.42,
             'pnl': -5.96, 'duration': 47, 'is_win': False, 'hour': 16, 'ts': 1774282400},
            # TRADE 4: LOSS - sopra centro, drift basso
            {'fp': 'LONG|FORTE|ALTA|SIDEWAYS', 'momentum': 'FORTE', 'volatility': 'ALTA',
             'trend': 'SIDEWAYS', 'direction': 'LONG', 'regime': 'RANGING',
             'rsi': 62, 'drift': 0.01, 'range_position': 0.65,
             'pnl': -1.81, 'duration': 45, 'is_win': False, 'hour': 16, 'ts': 1774282600},
            # TRADE 5: LOSS SHORT - in EXPLOSIVE
            {'fp': 'SHORT|MEDIO|ALTA|SIDEWAYS', 'momentum': 'MEDIO', 'volatility': 'ALTA',
             'trend': 'SIDEWAYS', 'direction': 'SHORT', 'regime': 'EXPLOSIVE',
             'rsi': 45, 'drift': 0.08, 'range_position': 0.45,
             'pnl': -6.13, 'duration': 21, 'is_win': False, 'hour': 16, 'ts': 1774283000},
            # TRADE 6: LOSS SHORT - in EXPLOSIVE
            {'fp': 'SHORT|MEDIO|ALTA|SIDEWAYS', 'momentum': 'MEDIO', 'volatility': 'ALTA',
             'trend': 'SIDEWAYS', 'direction': 'SHORT', 'regime': 'EXPLOSIVE',
             'rsi': 52, 'drift': 0.10, 'range_position': 0.50,
             'pnl': -5.70, 'duration': 20, 'is_win': False, 'hour': 16, 'ts': 1774283200},
        ], maxlen=200)

    def _fp(self, momentum: str, volatility: str, trend: str, direction: str = "LONG") -> str:
        return f"{direction}|{momentum}|{volatility}|{trend}"

    def _new_memory_entry(self):
        return {'wins': 0.0, 'samples': 0.0, 'pnl_sum': 0.0, 'real_samples': 0,
                'durations_win': deque(maxlen=50), 'durations_loss': deque(maxlen=50),
                'rsi_win': deque(maxlen=50), 'rsi_loss': deque(maxlen=50),
                'drift_win': deque(maxlen=50), 'drift_loss': deque(maxlen=50),
                'range_pos_win': deque(maxlen=50), 'range_pos_loss': deque(maxlen=50)}

    # -- LETTURA ----------------------------------------------------------

    def get_wr(self, momentum: str, volatility: str, trend: str, direction: str = "LONG") -> float:
        fp = self._fp(momentum, volatility, trend, direction)
        if fp not in self._memory or self._memory[fp]['samples'] < self.MIN_SAMPLES:
            return 0.72
        m = self._memory[fp]
        return m['wins'] / m['samples'] if m['samples'] > 0 else 0.72

    def get_pnl_avg(self, momentum: str, volatility: str, trend: str, direction: str = "LONG") -> float:
        fp = self._fp(momentum, volatility, trend, direction)
        if fp not in self._memory or self._memory[fp]['samples'] < self.MIN_SAMPLES:
            return 0.0
        m = self._memory[fp]
        return m.get('pnl_sum', 0) / m['samples'] if m['samples'] > 0 else 0.0

    def get_avg_duration(self, momentum: str, volatility: str, trend: str, 
                         direction: str = "LONG", is_win: bool = True) -> float:
        """Durata media dei WIN o LOSS per questo fingerprint. None se dati insufficienti."""
        fp = self._fp(momentum, volatility, trend, direction)
        mem = self._memory.get(fp)
        if not mem:
            return None
        key = 'durations_win' if is_win else 'durations_loss'
        durations = mem.get(key)
        if not durations or len(durations) < 3:
            return None
        return sum(durations) / len(durations)

    # -- FANTASMA (PNL-aware + WR) ----------------------------------------

    def is_fantasma(self, momentum: str, volatility: str, trend: str, direction: str = "LONG") -> tuple:
        fp  = self._fp(momentum, volatility, trend, direction)
        wr  = self.get_wr(momentum, volatility, trend, direction)
        pnl_avg = self.get_pnl_avg(momentum, volatility, trend, direction)
        mem = self._memory.get(fp, {})
        if mem.get('samples', 0) < self.MIN_SAMPLES:
            return False, ''
        if wr < self.FANTASMA_WR_THRESHOLD:
            return True, f"FANTASMA_WR fp={fp} wr={wr:.2f}"
        real_samples = mem.get('real_samples', 0)
        if real_samples >= self.MIN_REAL_SAMPLES and pnl_avg <= self.MIN_PNL_EDGE:
            return True, f"FANTASMA_PNL fp={fp} wr={wr:.2f} pnl_avg={pnl_avg:+.2f} real={real_samples}"
        return False, ''

    # -- CONTEXT-MATCHING - trova i trade passati più simili --------------

    def context_match(self, regime: str, momentum: str, volatility: str, trend: str,
                      direction: str, rsi: float, drift: float, range_position: float) -> dict:
        """
        Cerca i 5 trade passati più simili a questa situazione.
        Ritorna il PnL medio dei vicini e la predizione.
        """
        if len(self._trade_history) < 10:
            return {'pnl_predicted': 0, 'confidence': 0, 'neighbors': 0, 'verdict': 'DATI_INSUFFICIENTI'}

        # Calcola distanza pesata per ogni trade passato
        scored = []
        for t in self._trade_history:
            dist = 0.0
            # Regime match (peso 3)
            dist += (0 if t['regime'] == regime else 3.0)
            # Direction match (peso 2)
            dist += (0 if t['direction'] == direction else 2.0)
            # Momentum match (peso 2)
            mom_map = {'FORTE': 2, 'MEDIO': 1, 'DEBOLE': 0}
            dist += abs(mom_map.get(t['momentum'], 1) - mom_map.get(momentum, 1)) * 1.0
            # Volatility match (peso 1)
            vol_map = {'ALTA': 2, 'MEDIA': 1, 'BASSA': 0}
            dist += abs(vol_map.get(t['volatility'], 1) - vol_map.get(volatility, 1)) * 0.5
            # RSI distance (peso 1.5)
            dist += abs(t.get('rsi', 50) - rsi) / 20.0 * 1.5
            # Drift distance (peso 1.5)
            dist += abs(t.get('drift', 0) - drift) / 0.10 * 1.5
            # Range position (peso 2)
            dist += abs(t.get('range_position', 0.5) - range_position) * 2.0

            scored.append((dist, t))

        # I 5 più vicini
        scored.sort(key=lambda x: x[0])
        neighbors = scored[:5]
        
        if not neighbors:
            return {'pnl_predicted': 0, 'confidence': 0, 'neighbors': 0, 'verdict': 'NO_NEIGHBORS'}

        pnls = [t['pnl'] for _, t in neighbors]
        pnl_avg = sum(pnls) / len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        avg_dist = sum(d for d, _ in neighbors) / len(neighbors)
        confidence = max(0, min(1, 1.0 - avg_dist / 10.0))

        verdict = 'ENTRA' if pnl_avg > self.MIN_PNL_EDGE and wins >= 3 else 'BLOCCA'

        return {
            'pnl_predicted': round(pnl_avg, 2),
            'confidence': round(confidence, 2),
            'neighbors': len(neighbors),
            'wins': wins,
            'avg_distance': round(avg_dist, 2),
            'verdict': verdict,
        }

    # -- CAPSULE ORACOLO STATICHE (OC1-OC5) ------------------------------

    def check_capsules(self, regime, direction, rsi, drift, range_position, momentum, loss_streak) -> tuple:
        """
        5 capsule statiche dell'Oracolo. Ritorna (block, reason) o (False, '').
        """
        # OC1 - RANGING_MIDZONE: non tradare al centro del range
        if regime == "RANGING" and 0.40 <= range_position <= 0.60:
            return True, f"OC1_MIDZONE_{range_position:.0%}"

        # OC2 - RSI_EXTREME: non andare LONG in ipercomprato, SHORT in ipervenduto
        if direction == "LONG" and rsi > 75:
            return True, f"OC2_RSI_HIGH_{rsi:.0f}"
        if direction == "SHORT" and rsi < 25:
            return True, f"OC2_RSI_LOW_{rsi:.0f}"

        # OC3 - DRIFT_DIRECTION: soglia CONTESTUALE per regime
        # RANGING: drift oscilla per natura → soglia larga
        # TRENDING_*: drift è segnale vero → soglia stretta
        # EXPLOSIVE: movimento rapido → soglia media
        _oc3_thr = {"RANGING":-0.25,"TRENDING_BULL":-0.08,
                    "TRENDING_BEAR":-0.08,"EXPLOSIVE":-0.15}.get(regime,-0.15)
        if direction == "LONG" and drift < _oc3_thr:
            return True, f"OC3_DRIFT_{regime}_{drift:+.3f}(thr={_oc3_thr})"
        if direction == "SHORT" and drift > abs(_oc3_thr):
            return True, f"OC3_DRIFT_{regime}_{drift:+.3f}(thr={_oc3_thr})"

        # OC4 - MOMENTUM_RANGING: in RANGING FORTE senza drift = falso
        if regime == "RANGING" and momentum == "FORTE" and abs(drift) < 0.05:
            return True, f"OC4_FALSO_FORTE_drift{drift:+.3f}"

        # OC5 - LOSS_STREAK: dopo 5 loss, fermati
        if loss_streak >= 5:
            return True, f"OC5_LOSS_STREAK_{loss_streak}"

        # OC6 - RSI ESTREMO IN RANGING = rumore, non segnale
        # RSI > 72 in RANGING con SHORT: mercato ipercomprato ma laterale = instabile
        # RSI < 28 in RANGING con LONG: mercato ipervenduto ma laterale = instabile
        # In TRENDING questi RSI sono normali. In RANGING sono veleno.
        if regime == "RANGING" and direction == "SHORT" and rsi > 72:
            return True, f"OC6_RSI_RANGING_SHORT_{rsi:.0f}"
        if regime == "RANGING" and direction == "LONG" and rsi < 28:
            return True, f"OC6_RSI_RANGING_LONG_{rsi:.0f}"

        return False, ''

    # -- CAPSULE AUTO-GENERATIVE ------------------------------------------

    def maybe_generate_capsule(self, fp: str):
        """Genera capsule automatiche quando un fingerprint ha abbastanza dati."""
        mem = self._memory.get(fp)
        if not mem or mem.get('real_samples', 0) < 10:
            return
        
        wr = mem['wins'] / mem['samples'] if mem['samples'] > 0 else 0
        pnl_avg = mem.get('pnl_sum', 0) / mem['samples'] if mem['samples'] > 0 else 0
        
        # Pattern FORTE vincente: RSI basso + drift positivo + bordo basso
        if wr > 0.65 and pnl_avg > self.MIN_PNL_EDGE:
            rsi_wins = list(mem.get('rsi_win', []))
            drift_wins = list(mem.get('drift_win', []))
            rp_wins = list(mem.get('range_pos_win', []))
            
            if rsi_wins and drift_wins and rp_wins:
                avg_rsi = sum(rsi_wins) / len(rsi_wins)
                avg_drift = sum(drift_wins) / len(drift_wins)
                avg_rp = sum(rp_wins) / len(rp_wins)
                
                capsule = {
                    'fp': fp,
                    'type': 'WINNER_PATTERN',
                    'avg_rsi_win': round(avg_rsi, 1),
                    'avg_drift_win': round(avg_drift, 3),
                    'avg_range_pos_win': round(avg_rp, 2),
                    'wr': round(wr, 2),
                    'pnl_avg': round(pnl_avg, 2),
                    'samples': mem['real_samples'],
                    'created': time.time(),
                }
                # Non duplicare
                if not any(c['fp'] == fp and c['type'] == 'WINNER_PATTERN' for c in self._auto_capsules):
                    self._auto_capsules.append(capsule)
                    log.info(f"[ORACOLO] 🧬 CAPSULE AUTO: {fp} → WR {wr:.0%} pnl ${pnl_avg:+.2f} | "
                             f"RSI~{avg_rsi:.0f} drift~{avg_drift:+.3f} rpos~{avg_rp:.2f}")

        # Pattern TOSSICO: genera alert
        if wr < 0.30 and mem['real_samples'] >= 10:
            capsule = {
                'fp': fp,
                'type': 'TOXIC_PATTERN',
                'wr': round(wr, 2),
                'pnl_avg': round(pnl_avg, 2),
                'samples': mem['real_samples'],
                'created': time.time(),
            }
            if not any(c['fp'] == fp and c['type'] == 'TOXIC_PATTERN' for c in self._auto_capsules):
                self._auto_capsules.append(capsule)
                log.info(f"[ORACOLO] ☠️ TOXIC PATTERN: {fp} → WR {wr:.0%} pnl ${pnl_avg:+.2f}")

    # -- DURATION MEMORY - MIN_HOLD adattivo ------------------------------

    def get_dynamic_min_hold(self, momentum: str, volatility: str, trend: str,
                             direction: str = "LONG", regime: str = "RANGING") -> float:
        """
        MIN_HOLD completamente data-driven.

        Gerarchia dei dati (dal più specifico al più generale):
          1. Durata media WIN su questo fingerprint esatto (70%)
          2. Durata media WIN su tutti i fingerprint nello stesso regime (70%)
          3. Durata media di TUTTI i trade reali in memoria (60%)
          4. Zero — lascia decidere solo all'exit energy score

        Nessun numero fisso. La volpe impara dai propri trade.
        """
        # Livello 1: fingerprint specifico
        avg_dur = self.get_avg_duration(momentum, volatility, trend, direction, is_win=True)
        if avg_dur and avg_dur > 8:
            return avg_dur * 0.70

        # Livello 2: media WIN in tutto il regime corrente
        regime_wins = []
        for fp, m in self._memory.items():
            if regime.lower() in fp.lower() or True:  # tutti i pattern
                dw = m.get('durations_win')
                if dw and len(dw) >= 2:
                    regime_wins.extend(list(dw))
        if len(regime_wins) >= 3:
            avg_regime = sum(regime_wins) / len(regime_wins)
            if avg_regime > 8:
                return avg_regime * 0.70

        # Livello 3: media di tutti i trade reali
        all_durs = []
        for m in self._memory.values():
            dw = m.get('durations_win', [])
            dl = m.get('durations_loss', [])
            all_durs.extend(list(dw) + list(dl))
        if len(all_durs) >= 5:
            return (sum(all_durs) / len(all_durs)) * 0.60

        # Livello 4: nessun dato → 25 secondi minimi (evita EXIT_E15 prematuro)
        return 25.0

    # -- SCRITTURA - registra trade completo ------------------------------

    def record(self, momentum: str, volatility: str, trend: str, is_win: bool,
               direction: str = "LONG", pnl: float = 0.0, duration: float = 0.0,
               rsi: float = 50.0, drift: float = 0.0, range_position: float = 0.5,
               regime: str = "RANGING", hour: int = None):
        """Aggiorna memoria + salva trade completo per context-matching."""
        fp = self._fp(momentum, volatility, trend, direction)
        if fp not in self._memory:
            self._memory[fp] = self._new_memory_entry()
        m = self._memory[fp]
        
        # Decay
        m['wins']    *= self.DECAY_FACTOR
        m['samples'] *= self.DECAY_FACTOR
        m['pnl_sum']  = m.get('pnl_sum', 0.0) * self.DECAY_FACTOR
        
        # Nuovo dato
        m['wins']    += 1.0 if is_win else 0.0
        m['samples'] += 1.0
        m['pnl_sum'] += pnl
        m['real_samples'] = m.get('real_samples', 0) + 1

        # Memoria multi-dimensionale
        if is_win:
            m.setdefault('durations_win', deque(maxlen=50)).append(duration)
            m.setdefault('rsi_win', deque(maxlen=50)).append(rsi)
            m.setdefault('drift_win', deque(maxlen=50)).append(drift)
            m.setdefault('range_pos_win', deque(maxlen=50)).append(range_position)
        else:
            m.setdefault('durations_loss', deque(maxlen=50)).append(duration)
            m.setdefault('rsi_loss', deque(maxlen=50)).append(rsi)
            m.setdefault('drift_loss', deque(maxlen=50)).append(drift)
            m.setdefault('range_pos_loss', deque(maxlen=50)).append(range_position)

        # Trade history per context-matching
        self._trade_history.append({
            'fp': fp, 'momentum': momentum, 'volatility': volatility,
            'trend': trend, 'direction': direction, 'regime': regime,
            'rsi': rsi, 'drift': drift, 'range_position': range_position,
            'pnl': pnl, 'duration': duration, 'is_win': is_win,
            'hour': hour or datetime.utcnow().hour, 'ts': time.time(),
        })

        # Prova a generare capsule
        self.maybe_generate_capsule(fp)

        pnl_avg = m['pnl_sum'] / m['samples'] if m['samples'] > 0 else 0
        log.debug(f"[ORACOLO] {fp} → WR={m['wins']/m['samples']:.2f} pnl_avg={pnl_avg:+.2f} real={m['real_samples']}")

    # -- POST-TRADE TRACKER -----------------------------------------------

    def start_post_trade(self, fp: str, exit_price: float, direction: str):
        """Inizia il monitoraggio post-trade per 60 secondi."""
        self._post_trade_queue.append({
            'fp': fp, 'exit_price': exit_price, 'direction': direction,
            'start_time': time.time(), 'prices_after': [],
        })

    def update_post_trade(self, current_price: float):
        """Chiamato ogni tick - aggiorna i post-trade attivi."""
        to_close = []
        for i, pt in enumerate(self._post_trade_queue):
            elapsed = time.time() - pt['start_time']
            pt['prices_after'].append(current_price)
            
            if elapsed >= 60:
                # Valuta se il prezzo ha continuato nella direzione
                if pt['direction'] == 'LONG':
                    continued = current_price > pt['exit_price']
                    delta_after = current_price - pt['exit_price']
                else:
                    continued = current_price < pt['exit_price']
                    delta_after = pt['exit_price'] - current_price
                
                # Registra nell'Oracolo
                mem = self._memory.get(pt['fp'])
                if mem:
                    mem.setdefault('post_continued', deque(maxlen=50)).append(continued)
                    mem.setdefault('post_delta', deque(maxlen=50)).append(delta_after)
                
                if continued:
                    log.info(f"[POST-TRADE] ⚠️ {pt['fp']}: prezzo ha CONTINUATO +${delta_after:.0f} → exit era PRESTO")
                else:
                    log.info(f"[POST-TRADE] [OK] {pt['fp']}: prezzo ha INVERTITO ${delta_after:.0f} → exit era CORRETTA")
                
                to_close.append(i)
        
        for i in reversed(to_close):
            self._post_trade_queue.popleft() if i == 0 else None

    def get_exit_too_early_rate(self, fp: str) -> float:
        """% di volte che l'exit era troppo presto per questo fingerprint."""
        mem = self._memory.get(fp)
        if not mem or 'post_continued' not in mem or len(mem['post_continued']) < 3:
            return 0.5  # default neutro
        continued = list(mem['post_continued'])
        return sum(1 for c in continued if c) / len(continued)

    # -- DUMP -------------------------------------------------------------

    def dump(self) -> dict:
        result = {}
        for fp, m in self._memory.items():
            entry = {
                'wr': round(m['wins']/m['samples'], 3) if m['samples'] > 0 else 0,
                'pnl_avg': round(m.get('pnl_sum', 0)/m['samples'], 2) if m['samples'] > 0 else 0,
                'samples': round(m['samples'], 1),
                'real': m.get('real_samples', 0),
            }
            # Duration info
            dw = m.get('durations_win')
            if dw and len(dw) > 0:
                entry['dur_win_avg'] = round(sum(dw)/len(dw), 1)
            dl = m.get('durations_loss')
            if dl and len(dl) > 0:
                entry['dur_loss_avg'] = round(sum(dl)/len(dl), 1)
            # Post-trade info
            pc = m.get('post_continued')
            if pc and len(pc) > 0:
                entry['exit_too_early'] = round(sum(1 for c in pc if c)/len(pc), 2)
            result[fp] = entry
        
        result['_auto_capsules'] = len(self._auto_capsules)
        result['_trade_history'] = len(self._trade_history)
        return result

# ===========================================================================
# PRE-TRADE SIGNAL TRACKER
# ===========================================================================
# Il tracker speculare al phantom.
#
# Il phantom misura cosa succede quando il sistema NON entra.
# Il PreTradeSignalTracker misura cosa succede quando il sistema VUOLE entrare.
#
# Ogni segnale (score ≥ soglia) viene registrato con:
#   - prezzo, direzione, regime, score, momentum, rsi, macd
#   - poi segue il prezzo per 30s / 60s / 120s
#   - misura delta_30, delta_60, delta_120 nella direzione prevista
#
# Dopo 50 segnali emerge la distribuzione reale:
#   "LONG score 65+ in RANGING → prezzo sale $8 in 60s nel 68% dei casi"
#
# Questo è il MOTORE PREVISIONALE. Non "entro o non entro" —
# "il mercato si muoverà di X in Y secondi con probabilità Z".
# ===========================================================================

class PreTradeSignalTracker:
    """
    Traccia ogni segnale di entry (score ≥ soglia) e misura
    il movimento reale del prezzo nelle successive 30/60/120 secondi.

    Costruisce la distribuzione previsionale del sistema:
    per ogni contesto (regime, direction, score_band) → P(movimento > X in T secondi)
    """

    WINDOWS = [30, 60, 120]  # secondi di osservazione post-segnale
    MAX_OPEN  = 20            # max segnali aperti simultanei
    MAX_CLOSED = 500          # ultimi N segnali chiusi in memoria

    def __init__(self):
        self._open:   list         = []                    # segnali aperti
        self._closed: deque        = deque(maxlen=self.MAX_CLOSED)
        self._stats:  dict         = defaultdict(lambda: {
            'n': 0,
            'delta_30':  [], 'delta_60':  [], 'delta_120': [],
            'hit_30':    [], 'hit_60':    [], 'hit_120':   [],  # True = prezzo andato nella dir giusta
            'pnl_sim':   [],  # PnL simulato con fee
        })

    def record_signal(self, price: float, direction: str, score: float,
                      soglia: float, regime: str, momentum: str,
                      volatility: str, trend: str, rsi: float,
                      macd_hist: float, drift: float):
        """
        Registra segnale se score >= 25.
        Soglia bassa = più dati = distribuzione previsionale più ricca.
        """
        if score < 25:
            return  # sotto 25 è rumore puro
        if len(self._open) >= self.MAX_OPEN:
            return  # non sovraccaricare

        # Score band: categorizza lo score per analisi statistica
        if score >= 75:
            score_band = "FORTE_75+"
        elif score >= 65:
            score_band = "BUONO_65-75"
        elif score >= 58:
            score_band = "BASE_58-65"
        else:
            score_band = "DEBOLE_<58"

        signal = {
            'price':      price,
            'direction':  direction,
            'score':      score,
            'soglia':     soglia,
            'score_band': score_band,
            'regime':     regime,
            'momentum':   momentum,
            'volatility': volatility,
            'trend':      trend,
            'rsi':        rsi,
            'macd_hist':  macd_hist,
            'drift':      drift,
            'ts':         time.time(),
            'prices':     [],           # prezzi raccolti
            'closed':     False,
            'results':    {},           # delta_30, delta_60, delta_120
        }
        self._open.append(signal)

    def update(self, current_price: float):
        """Chiamato ogni tick. Aggiorna i segnali aperti."""
        now     = time.time()
        to_close = []

        for i, sig in enumerate(self._open):
            elapsed = now - sig['ts']
            sig['prices'].append(current_price)

            # Calcola risultati alle finestre temporali
            for w in self.WINDOWS:
                key = f'delta_{w}'
                if key not in sig['results'] and elapsed >= w:
                    if sig['direction'] == 'LONG':
                        delta = current_price - sig['price']
                    else:
                        delta = sig['price'] - current_price
                    # Fee simulata con size reale: ~$250 × 0.02% × 2 = $0.10
                    # Size reale = capital × size_factor × 0.05 ≈ $250
                    _size_sim = 250.0  # stima conservativa size reale
                    _fee_sim  = _size_sim * 0.0002 * 2  # 0.02% maker × 2 lati
                    pnl_sim = delta * (_size_sim / sig['price']) - _fee_sim
                    sig['results'][key]          = round(delta, 2)
                    sig['results'][f'pnl_{w}']   = round(pnl_sim, 2)
                    # Fee simulata: $5000 esposti × 0.02% × 2 = $2
                    pnl_sim = delta * (5000 / sig['price']) - 2.0
                    sig['results'][f'hit_{w}']    = delta > 0

            # Chiudi dopo la finestra massima
            if elapsed >= max(self.WINDOWS):
                to_close.append(i)

        for i in reversed(to_close):
            sig = self._open.pop(i)
            sig['closed'] = True
            self._closed.append(sig)
            self._update_stats(sig)

    def _update_stats(self, sig: dict):
        """Aggiorna la distribuzione statistica dopo ogni segnale chiuso."""
        # Chiave per la distribuzione: regime + direction + score_band
        key = f"{sig['regime']}|{sig['direction']}|{sig['score_band']}"
        s   = self._stats[key]
        s['n'] += 1

        for w in self.WINDOWS:
            d   = sig['results'].get(f'delta_{w}')
            h   = sig['results'].get(f'hit_{w}')
            pnl = sig['results'].get(f'pnl_{w}')
            if d is not None:
                s[f'delta_{w}'].append(d)
                s[f'hit_{w}'].append(h)
            if pnl is not None:
                s['pnl_sim'].append(pnl)

        # Mantieni solo ultimi 100 per ogni chiave
        all_fields = ([f'delta_{w}' for w in self.WINDOWS] +
                      [f'hit_{w}'   for w in self.WINDOWS] + ['pnl_sim'])
        for field in all_fields:
            if field in s and len(s[field]) > 100:
                s[field] = s[field][-100:]

    def get_prediction(self, direction: str, score: float,
                       regime: str) -> dict:
        """
        Ritorna la predizione per questo contesto.
        "Se il sistema dice LONG con score 65 in RANGING, quanto si muove?"
        """
        if score >= 75:   band = "FORTE_75+"
        elif score >= 65: band = "BUONO_65-75"
        elif score >= 58: band = "BASE_58-65"
        else:             band = "DEBOLE_<58"

        key = f"{regime}|{direction}|{band}"
        s   = self._stats.get(key)

        if not s or s['n'] < 5:
            return {'confidence': 0, 'data_insufficienti': True, 'n': s['n'] if s else 0}

        result = {'n': s['n'], 'context': key}
        for w in self.WINDOWS:
            deltas = s.get(f'delta_{w}', [])
            hits   = s.get(f'hit_{w}',   [])
            if deltas:
                result[f'avg_delta_{w}s']  = round(sum(deltas)/len(deltas), 2)
                result[f'hit_rate_{w}s']   = round(sum(hits)/len(hits), 3) if hits else 0
                result[f'max_delta_{w}s']  = round(max(deltas), 2)

        pnls = s.get('pnl_sim', [])
        if pnls:
            result['avg_pnl_sim']  = round(sum(pnls)/len(pnls), 2)
            result['pnl_positive'] = round(sum(1 for p in pnls if p > 0)/len(pnls), 3)

        # Confidence: cresce con n campioni, max 1.0 a 50 campioni
        result['confidence'] = min(1.0, s['n'] / 50)
        return result

    def dump_top(self, n: int = 10) -> list:
        """Top N contesti per numero di segnali — per la dashboard."""
        rows = []
        for key, s in self._stats.items():
            if s['n'] < 1:
                continue
            hits_60 = s.get('hit_60', [])
            deltas_60 = s.get('delta_60', [])
            rows.append({
                'context':    key,
                'n':          s['n'],
                'hit_60s':    round(sum(hits_60)/len(hits_60), 3) if hits_60 else 0,
                'avg_delta_60s': round(sum(deltas_60)/len(deltas_60), 2) if deltas_60 else 0,
                'pnl_sim_avg': round(sum(s['pnl_sim'])/len(s['pnl_sim']), 2) if s['pnl_sim'] else 0,
            })
        rows.sort(key=lambda x: x['n'], reverse=True)
        return rows[:n]

    def predict_from_signals(self, regime: str, direction: str,
                              score: float, drift: float,
                              rsi: float) -> dict:
        """
        Predizione basata sui segnali storici chiusi.
        Cerca i segnali più simili e predice hit_rate e delta.

        Questo è l'Oracolo predittivo — anticipa prima che accada,
        non reagisce a quello che è già successo.
        """
        if len(self._closed) < 20:
            return {'confidence': 0, 'hit_rate': 0.5,
                    'avg_delta': 0, 'n_vicini': 0,
                    'verdict': 'DATI_INSUFFICIENTI'}

        # Cerca vicini per distanza pesata
        vicini = []
        for sig in self._closed:
            if sig.get('direction') != direction: continue
            if sig.get('regime')    != regime:    continue

            # Distanza su score, drift, rsi
            d_score = abs(sig.get('score', 50) - score) / 10.0
            d_drift = abs(sig.get('drift', 0)  - drift) / 0.05
            d_rsi   = abs(sig.get('rsi',   50) - rsi)   / 15.0

            dist = d_score * 2.0 + d_drift * 1.5 + d_rsi * 1.0

            # Solo vicini abbastanza simili
            if dist > 4.0: continue

            h60  = sig.get('results', {}).get('hit_60',  None)
            d60  = sig.get('results', {}).get('delta_60', None)
            p60  = sig.get('results', {}).get('pnl_60',   None)
            if h60 is None: continue

            vicini.append({'dist': dist, 'hit': h60, 'delta': d60 or 0, 'pnl': p60})

        if len(vicini) < 5:
            return {'confidence': 0, 'hit_rate': 0.5,
                    'avg_delta': 0, 'n_vicini': len(vicini),
                    'verdict': 'VICINI_INSUFFICIENTI'}

        # Peso inverso alla distanza
        tot_peso = sum(1/(v['dist']+0.1) for v in vicini)
        hit_rate  = sum((1/(v['dist']+0.1))*v['hit']   for v in vicini) / tot_peso
        avg_delta = sum((1/(v['dist']+0.1))*v['delta'] for v in vicini) / tot_peso

        # Confidence: cresce con n_vicini, max a 50
        confidence = min(1.0, len(vicini) / 50)

        # CRITERIO ECONOMICO EMERGENTE — nessuna soglia fissa
        # Fee simulata nella stessa scala di pnl_60: $250 * 0.02% * 2 = $0.10
        # hit_economica = % vicini con pnl_60 > fee_sim (coprono davvero i costi)
        # Il numero emerge dalla distribuzione storica dei vicini — non è inventato
        FEE_SIM = 0.10  # fee nella scala simulata (size=$250)
        pnl_vicini = [v['pnl'] for v in vicini if v.get('pnl') is not None]

        if pnl_vicini:
            hit_econ = sum(1 for p in pnl_vicini if p > FEE_SIM) / len(pnl_vicini)
            pnl_medio = sum(pnl_vicini) / len(pnl_vicini)
            # ENTRA: maggioranza dei vicini copre davvero le fee E hit direzionale ok
            if hit_econ >= 0.50 and hit_rate >= 0.60:
                verdict = 'ENTRA'
            # BLOCCA: meno di 1/3 dei vicini copre le fee O hit direzionale basso
            elif hit_econ < 0.30 or hit_rate <= 0.40:
                verdict = 'BLOCCA'
            else:
                verdict = 'NEUTRO'
        else:
            # Fallback senza pnl: solo hit_rate + delta conservativo
            if hit_rate >= 0.65 and avg_delta > 20:
                verdict = 'ENTRA'
            elif hit_rate <= 0.40 or avg_delta < -5:
                verdict = 'BLOCCA'
            else:
                verdict = 'NEUTRO'

        return {
            'confidence': round(confidence, 2),
            'hit_rate':   round(hit_rate,   3),
            'avg_delta':  round(avg_delta,  2),
            'n_vicini':   len(vicini),
            'verdict':    verdict,
        }

    def get_open_count(self) -> int:
        return len(self._open)


# ===========================================================================
# 5 CAPSULE INTELLIGENTI
# ===========================================================================

class Capsule1Coerenza:
    """Valida coerenza tra fingerprint_wr e contesto attuale."""
    def valida(self, fingerprint_wr, momentum, volatility, trend,
               soglia_buona=0.60, soglia_perfetta=0.75):
        if fingerprint_wr > soglia_perfetta and momentum == "FORTE" and volatility == "BASSA" and trend == "UP":
            return True, 0.95, "COERENZA PERFETTA"
        if fingerprint_wr > soglia_buona and momentum in ("FORTE", "MEDIO") and trend == "UP":
            return True, fingerprint_wr, "COERENZA BUONA"
        return False, 0.10, "BLOCCO_COERENZA"

class Capsule2Trappola:
    """Riconosce setup trappola da confidence bassa."""
    def riconosci(self, confidence):
        if confidence < 0.50:
            return False, "TRAPPOLA_CONFIDENCE"
        return True, "OK"

class Capsule3Protezione:
    """Blocca in condizioni di alta volatilita con impulso debole."""
    def proteggi(self, momentum, volatility, fingerprint_wr, fp_minimo=0.55):
        if momentum == "DEBOLE" and volatility == "ALTA" and fingerprint_wr <= 0.70:
            return False, "PROTETTO_VOLATILITÀ"
        if volatility == "ALTA" and fingerprint_wr < fp_minimo:
            return False, "PROTETTO_FP_BASSO"
        return True, "OK"

class Capsule4Opportunita:
    """Riconosce finestre di opportunita premium."""
    def riconosci(self, fingerprint_wr, momentum, volatility, soglia_buona=0.65):
        if fingerprint_wr > 0.75 and momentum == "FORTE" and volatility == "BASSA":
            return True, 0.95, "OPPORTUNITÀ_ORO"
        if fingerprint_wr > soglia_buona and momentum == "FORTE":
            return True, fingerprint_wr, "OPPORTUNITÀ_BUONA"
        return False, 0.40, "NO_OPPORTUNITÀ"

class Capsule5Tattica:
    """Timing tattico: entry solo se coerenza e confidence alte."""
    def timing(self, entry_trigger, coerenza, confidence, conf_ok=0.65):
        if entry_trigger and coerenza and confidence > 0.80:
            return True, 45, "TIMING_PERFETTO"
        if entry_trigger and confidence > conf_ok:
            return True, 25, "TIMING_OK"
        return False, 0, "TIMING_NO"

# ===========================================================================
# MATRIMONI INTELLIGENTI - 7 TIPI
# ===========================================================================

class MatrimonioIntelligente:
    """
    7 matrimoni con WR atteso e duration media.
    La chiave è (momentum, volatility, trend).
    """
    MARRIAGES = {
        # -- TREND UP -----------------------------------------------------
        ("FORTE", "BASSA",  "UP"):      {"name": "STRONG_BULL",    "wr": 0.85, "duration_avg": 45, "confidence": 0.95},
        ("FORTE", "MEDIA",  "UP"):      {"name": "STRONG_MED",     "wr": 0.75, "duration_avg": 30, "confidence": 0.85},
        ("FORTE", "ALTA",   "UP"):      {"name": "STRONG_VOLATILE","wr": 0.65, "duration_avg": 20, "confidence": 0.70},
        ("MEDIO", "BASSA",  "UP"):      {"name": "MEDIUM_BULL",    "wr": 0.70, "duration_avg": 25, "confidence": 0.80},
        ("MEDIO", "MEDIA",  "UP"):      {"name": "CAUTIOUS",       "wr": 0.60, "duration_avg": 15, "confidence": 0.65},
        ("MEDIO", "ALTA",   "UP"):      {"name": "CAUTIOUS_VOL",   "wr": 0.50, "duration_avg": 12, "confidence": 0.55},
        ("DEBOLE","BASSA",  "UP"):      {"name": "WEAK_BULL",      "wr": 0.55, "duration_avg": 15, "confidence": 0.55},
        ("DEBOLE","MEDIA",  "UP"):      {"name": "WEAK_MED_UP",    "wr": 0.45, "duration_avg": 10, "confidence": 0.45},
        ("DEBOLE","ALTA",   "UP"):      {"name": "WEAK_VOL_UP",    "wr": 0.35, "duration_avg": 8,  "confidence": 0.35},
        # -- TREND SIDEWAYS -----------------------------------------------
        # CALIBRATO su 500+ trade reali (sessioni 22-23 marzo 2026)
        ("FORTE", "BASSA",  "SIDEWAYS"):{"name": "RANGE_STRONG",   "wr": 0.65, "duration_avg": 45, "confidence": 0.70},
        ("FORTE", "MEDIA",  "SIDEWAYS"):{"name": "RANGE_MED_F",    "wr": 0.60, "duration_avg": 40, "confidence": 0.65},
        ("FORTE", "ALTA",   "SIDEWAYS"):{"name": "RANGE_VOL_F",    "wr": 0.60, "duration_avg": 35, "confidence": 0.60},
        ("MEDIO", "BASSA",  "SIDEWAYS"):{"name": "RANGE_CALM",     "wr": 0.50, "duration_avg": 35, "confidence": 0.55},
        ("MEDIO", "MEDIA",  "SIDEWAYS"):{"name": "RANGE_NEUTRAL",  "wr": 0.45, "duration_avg": 30, "confidence": 0.45},
        ("MEDIO", "ALTA",   "SIDEWAYS"):{"name": "RANGE_VOL_M",    "wr": 0.43, "duration_avg": 30, "confidence": 0.40},
        ("DEBOLE","BASSA",  "SIDEWAYS"):{"name": "RANGE_DEAD",     "wr": 0.35, "duration_avg": 25, "confidence": 0.30},
        ("DEBOLE","MEDIA",  "SIDEWAYS"):{"name": "WEAK_NEUTRAL",   "wr": 0.35, "duration_avg": 25, "confidence": 0.30},
        ("DEBOLE","ALTA",   "SIDEWAYS"):{"name": "RANGE_VOL_W",    "wr": 0.19, "duration_avg": 20, "confidence": 0.15},
        # -- TREND DOWN ---------------------------------------------------
        ("FORTE", "BASSA",  "DOWN"):    {"name": "BEAR_STRONG",    "wr": 0.60, "duration_avg": 20, "confidence": 0.65},
        ("FORTE", "MEDIA",  "DOWN"):    {"name": "BEAR_MED_F",     "wr": 0.50, "duration_avg": 15, "confidence": 0.55},
        ("FORTE", "ALTA",   "DOWN"):    {"name": "PANIC",          "wr": 0.15, "duration_avg": 3,  "confidence": 0.15},
        ("MEDIO", "BASSA",  "DOWN"):    {"name": "BEAR_CALM",      "wr": 0.45, "duration_avg": 12, "confidence": 0.50},
        ("MEDIO", "MEDIA",  "DOWN"):    {"name": "BEAR_NEUTRAL",   "wr": 0.40, "duration_avg": 10, "confidence": 0.40},
        ("MEDIO", "ALTA",   "DOWN"):    {"name": "BEAR_VOL",       "wr": 0.30, "duration_avg": 8,  "confidence": 0.30},
        ("DEBOLE","BASSA",  "DOWN"):    {"name": "BEAR_WEAK",      "wr": 0.35, "duration_avg": 8,  "confidence": 0.35},
        ("DEBOLE","MEDIA",  "DOWN"):    {"name": "BEAR_WEAK_M",    "wr": 0.25, "duration_avg": 5,  "confidence": 0.25},
        ("DEBOLE","ALTA",   "DOWN"):    {"name": "TRAP",           "wr": 0.05, "duration_avg": 2,  "confidence": 0.05},
    }

    @staticmethod
    def get_marriage(momentum, volatility, trend):
        key = (momentum, volatility, trend)
        return MatrimonioIntelligente.MARRIAGES.get(key, {
            "name": "UNKNOWN", "wr": 0.50, "duration_avg": 12, "confidence": 0.50
        })

    @staticmethod
    def get_by_name(name: str) -> dict:
        for m in MatrimonioIntelligente.MARRIAGES.values():
            if m["name"] == name:
                return m
        return {"name": name, "wr": 0.50, "duration_avg": 12, "confidence": 0.50}

# ===========================================================================
# MEMORIA MATRIMONI - trust, separazione, divorzio
# ===========================================================================

class MemoriaMatrimoni:
    """
    Tiene traccia delle performance per ogni matrimonio.
    - trust [0–100]: sale con win (+5), scende con loss (-15)
    - SEPARAZIONE: WR reale < 60% dell'atteso dopo 10 trade → blacklist 50 trade
    - DIVORZIO PERMANENTE: seconda SEPARAZIONE → fuori per sempre
    """

    def __init__(self):
        self.trust      = defaultdict(lambda: 50)
        self.separazione= defaultdict(bool)
        self.blacklist  = defaultdict(int)
        self.divorzio   = set()
        self.wr_history = defaultdict(list)
        self.wins       = defaultdict(int)
        self.losses     = defaultdict(int)

    def get_status(self, name: str) -> tuple:
        if name in self.divorzio:
            return False, "DIVORZIO_PERMANENTE"
        if self.blacklist[name] > 0:
            self.blacklist[name] -= 1
            return False, f"SEPARAZIONE_ATTIVA ({self.blacklist[name]} rimasti)"
        if self.trust[name] < 30:
            return False, f"TRUST_BASSO ({self.trust[name]})"
        return True, "OK"

    def get_wr(self, name: str) -> float:
        total = self.wins.get(name, 0) + self.losses.get(name, 0)
        return self.wins.get(name, 0) / total if total > 0 else 0.5

    def get_trust(self, name: str) -> float:
        return self.trust.get(name, 50) / 100.0

    def record_trade(self, name: str, is_win: bool, wr_expected: float):
        if is_win:
            self.wins[name]  += 1
            self.trust[name] = min(100, self.trust[name] + 5)
        else:
            self.losses[name]  += 1
            self.trust[name]   = max(0, self.trust[name] - 15)

        total = self.wins[name] + self.losses[name]
        if total > 0:
            wr_reale = self.wins[name] / total
            self.wr_history[name].append(wr_reale)
            if len(self.wr_history[name]) >= 10:
                recent_wr = sum(self.wr_history[name][-10:]) / 10
                if recent_wr < wr_expected * 0.6:
                    if self.separazione[name]:
                        self.divorzio.add(name)
                        self.trust[name] = 0
                        log.warning(f"[DIVORZIO PERMANENTE] 💔 {name} eliminato")
                    else:
                        self.separazione[name] = True
                        self.blacklist[name]   = 50
                        log.warning(f"[SEPARAZIONE] ⚠️  {name} blacklist 50 trade")

# ===========================================================================
# ANALIZZATORE CONTESTO
# ===========================================================================

class ContestoAnalyzer:
    """Momentum, volatility, trend dai prezzi recenti."""

    def __init__(self, window: int = 50):
        self.prices    = deque(maxlen=window)
        self.tick_count= 0

    def add_price(self, price: float):
        self.prices.append(price)
        self.tick_count += 1

    def analyze(self, regime=None, drift=None):
        if len(self.prices) < 10:
            return None, None, None
        prices    = list(self.prices)
        recent    = prices[-5:]
        changes   = [recent[i+1] - recent[i] for i in range(len(recent)-1)]
        up_count  = sum(1 for c in changes if c > 0)
        momentum  = "FORTE" if up_count >= 4 else ("MEDIO" if up_count >= 2 else "DEBOLE")

        r20        = prices[-20:]
        changes20  = [abs(r20[i+1] - r20[i]) for i in range(len(r20)-1)]
        avg_ch20   = sum(changes20) / len(changes20) if changes20 else 0
        volatility = "ALTA" if avg_ch20 > 0.005 else ("MEDIA" if avg_ch20 > 0.002 else "BASSA")

        chg_pct = (prices[-1] - prices[0]) / prices[0] * 100
        trend   = "UP" if chg_pct > 0.3 else ("DOWN" if chg_pct < -0.3 else "SIDEWAYS")

        # -- RANGING DOWNGRADE: FORTE in laterale senza direzione = falso --
        # 4 tick su = FORTE, ma in RANGING con drift ~0 è solo rumore.
        # Declassa solo se drift conferma assenza di direzione reale.
        # NON declassare se drift è forte (impulso vero al bordo del range).
        if regime == "RANGING" and trend == "SIDEWAYS" and drift is not None:
            if abs(drift) < 0.10:  # drift sotto 0.10% = nessuna direzione
                if momentum == "FORTE":
                    momentum = "MEDIO"
                elif momentum == "MEDIO":
                    momentum = "DEBOLE"

        return momentum, volatility, trend

# ===========================================================================
# PERSISTENZA SQLite - capital e trades sopravvivono al restart
# ===========================================================================

def _calcola_soglia_da_signal_tracker(bot) -> dict:
    """
    Calcola la soglia ottimale dai dati reali del Signal Tracker.
    Usa hit_rate e PnL reale — non regime fisso.
    La soglia emerge dai dati — non è mai un numero scritto a mano.
    """
    try:
        if not hasattr(bot, 'signal_tracker'):
            return {'base': 52, 'min': 48, 'motivo': 'NO_TRACKER'}

        stats = getattr(bot.signal_tracker, '_stats', {})
        if not stats:
            return {'base': 52, 'min': 48, 'motivo': 'NO_DATA'}

        # Raccoglie tutti i contesti con abbastanza campioni
        contesti = []
        for ctx, s in stats.items():
            hits = s.get('hit_60', [])
            pnls = s.get('pnl_sim', [])
            n = len(hits)
            if n < 20:  # minimo 20 campioni
                continue
            hit_rate = sum(hits) / n
            pnl_avg  = sum(pnls) / len(pnls) if pnls else 0
            contesti.append({
                'ctx': ctx, 'n': n,
                'hit_rate': hit_rate,
                'pnl_avg': pnl_avg
            })

        if not contesti:
            return {'base': 52, 'min': 48, 'motivo': 'POCHI_DATI'}

        # Media pesata per n campioni
        tot_n    = sum(c['n'] for c in contesti)
        avg_hit  = sum(c['hit_rate'] * c['n'] for c in contesti) / tot_n
        avg_pnl  = sum(c['pnl_avg']  * c['n'] for c in contesti) / tot_n

        # Soglia proporzionale all'hit rate reale
        # hit 65%+ → soglia 46/42  (mercato favorevole)
        # hit 60%+  → soglia 48/44
        # hit 55%+  → soglia 50/46
        # hit <55%  → soglia 52/48 (conservativo)
        if avg_hit >= 0.65 and avg_pnl > 0:
            base, min_s = 46, 42
            motivo = f"OTTIMO hit={avg_hit:.0%} pnl={avg_pnl:+.2f} n={tot_n}"
        elif avg_hit >= 0.60 and avg_pnl > 0:
            base, min_s = 48, 44
            motivo = f"BUONO hit={avg_hit:.0%} pnl={avg_pnl:+.2f} n={tot_n}"
        elif avg_hit >= 0.55:
            base, min_s = 50, 46
            motivo = f"DISCRETO hit={avg_hit:.0%} n={tot_n}"
        else:
            base, min_s = 52, 48
            motivo = f"STANDARD hit={avg_hit:.0%} n={tot_n}"

        return {'base': base, 'min': min_s, 'motivo': motivo}

    except Exception as e:
        log.error(f"[SOGLIA_DINAMICA] Errore: {e}")
        return {'base': 52, 'min': 48, 'motivo': 'ERRORE fallback'}


class PersistenzaStato:
    """Legge/scrive capital e total_trades su SQLite."""

    DEFAULT_CAPITAL = 10000.0
    DEFAULT_TRADES  = 0

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._ensure_dir()
        self._init_db()

    def _ensure_dir(self):
        d = os.path.dirname(self.db_path)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)

    def _init_db(self):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"[PERSIST] Init DB: {e}")

    def load(self) -> tuple:
        """Ritorna (capital, total_trades)."""
        try:
            conn = sqlite3.connect(self.db_path)
            rows = dict(conn.execute("SELECT key, value FROM bot_state").fetchall())
            conn.close()
            capital      = float(rows.get('capital',      self.DEFAULT_CAPITAL))
            total_trades = int(rows.get('total_trades',   self.DEFAULT_TRADES))
            log.info(f"[PERSIST] Stato caricato: capital={capital:.2f} trades={total_trades}")
            return capital, total_trades
        except Exception as e:
            log.error(f"[PERSIST] Load: {e} - uso defaults")
            return self.DEFAULT_CAPITAL, self.DEFAULT_TRADES

    def save_brain(self, oracolo, memoria, calibratore):
        """
        Serializza l'intelligenza accumulata su SQLite.
        OracoloDinamico + MemoriaMatrimoni + AutoCalibratore params.
        Chiamato ad ogni trade chiuso e ogni 5 minuti.
        """
        try:
            import json
            conn = sqlite3.connect(self.db_path)

            # -- OracoloDinamico 2.0 --------------------------------------
            # Serializza _memory con deque → list per JSON
            oracolo_data = {}
            for fp, m in oracolo._memory.items():
                entry = {}
                for k, v in m.items():
                    if isinstance(v, deque):
                        entry[k] = list(v)
                    else:
                        entry[k] = v
                oracolo_data[fp] = entry
            conn.execute("INSERT OR REPLACE INTO bot_state VALUES ('oracolo', ?)",
                        (json.dumps(oracolo_data),))

            # -- MemoriaMatrimoni ----------------------------------------
            memoria_data = {
                'trust':      dict(memoria.trust),
                'separazione':dict(memoria.separazione),
                'blacklist':  dict(memoria.blacklist),
                'divorzio':   list(memoria.divorzio),
                'wins':       dict(memoria.wins),
                'losses':     dict(memoria.losses),
                'wr_history': {k: list(v) for k, v in memoria.wr_history.items()},
            }
            conn.execute("INSERT OR REPLACE INTO bot_state VALUES ('memoria', ?)",
                        (json.dumps(memoria_data),))

            # -- AutoCalibratore params -----------------------------------
            conn.execute("INSERT OR REPLACE INTO bot_state VALUES ('calibra_params', ?)",
                        (json.dumps(calibratore.params),))

            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"[BRAIN_SAVE] {e}")

    def save_runtime_state(self, bot):
        """
        Persiste TUTTO lo stato runtime che ha valore statistico.
        Chiamato ogni 5 minuti. Zero dati preziosi persi tra deploy.
        """
        try:
            data = {
                # Phantom stats — storico protezioni/zavorre
                'phantom_stats': bot._phantom_stats,

                # Ultimi 100 fantasmi chiusi
                'phantoms_closed': [
                    {k: v for k, v in ph.items()
                     if k not in ('prices',)} # escludi liste grandi
                    for ph in list(bot._phantoms_closed)
                ],

                # Trade buffer IntelligenzaAutonoma
                'ia_trade_buffer': list(bot.realtime_engine._trade_buffer),

                # PreBreakout results per auto-tune
                'pb3_results': list(bot.campo._pb3_results)
                    if hasattr(bot.campo, '_pb3_results') else [],

                # Ultimi risultati campo per history_factor
                'campo_recent_results': list(bot.campo._recent_results)
                    if hasattr(bot.campo, '_recent_results') else [],

                # State engine — ultimi trade M2
                'm2_recent_trades': list(bot._m2_recent_trades),

                # Contatori M2
                'm2_wins':   bot._m2_wins,
                'm2_losses': bot._m2_losses,
                'm2_pnl':    bot._m2_pnl,
                'm2_trades': bot._m2_trades,
                # Pesi SC — sopravvivono ai restart
                'sc_pesi': bot.supercervello._pesi if hasattr(bot,'supercervello') else {},
                'sc_storia_n': len(bot.supercervello._storia) if hasattr(bot,'supercervello') else 0,
                # NOTA: soglia NON salvata — viene calcolata dinamicamente dal Signal Tracker
                # Veritas — salva segnali chiusi e statistiche
                'veritas_closed': [
                    {k:v for k,v in s.items() if k != 'deltas'}
                    for s in list(bot.veritas._closed)[-200:]
                ] if hasattr(bot, 'veritas') else [],
                'veritas_stats': bot.veritas._stats if hasattr(bot, 'veritas') else {},
            }
            conn = sqlite3.connect(self.db_path)
            conn.execute("INSERT OR REPLACE INTO bot_state VALUES ('runtime_state', ?)",
                        (json.dumps(data, default=str),))
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"[RUNTIME_SAVE] {e}")

    def load_runtime_state(self, bot):
        """Ripristina lo stato runtime dal DB."""
        try:
            conn = sqlite3.connect(self.db_path)
            rows = dict(conn.execute(
                "SELECT key, value FROM bot_state WHERE key='runtime_state'"
            ).fetchall())
            conn.close()

            if 'runtime_state' not in rows:
                return

            data = json.loads(rows['runtime_state'])
            restored = []

            # Phantom stats
            if 'phantom_stats' in data:
                for k, v in data['phantom_stats'].items():
                    if k not in bot._phantom_stats:
                        bot._phantom_stats[k] = v
                restored.append(f"phantom_stats:{len(data['phantom_stats'])}")

            # IA trade buffer
            if 'ia_trade_buffer' in data:
                for t in data['ia_trade_buffer']:
                    bot.realtime_engine._trade_buffer.append(t)
                restored.append(f"ia_buffer:{len(data['ia_trade_buffer'])}")

            # PreBreakout results
            if 'pb3_results' in data and hasattr(bot.campo, '_pb3_results'):
                for r in data['pb3_results']:
                    bot.campo._pb3_results.append(r)
                restored.append(f"pb3:{len(data['pb3_results'])}")

            # Campo recent results
            if 'campo_recent_results' in data and hasattr(bot.campo, '_recent_results'):
                for r in data['campo_recent_results']:
                    bot.campo._recent_results.append(r)
                restored.append(f"campo_recent:{len(data['campo_recent_results'])}")

            # State engine recent trades
            if 'm2_recent_trades' in data:
                for t in data['m2_recent_trades']:
                    bot._m2_recent_trades.append(t)
                restored.append(f"m2_trades:{len(data['m2_recent_trades'])}")

            # Ripristina pesi SC
            if 'sc_pesi' in data and hasattr(bot, 'supercervello') and data['sc_pesi']:
                pesi_caricati = data['sc_pesi']
                # Applica pavimento — campo_carica non può essere sotto 30%
                if pesi_caricati.get('campo_carica', 0) < 0.30:
                    log.warning("[RUNTIME_LOAD] ⚠️ Pesi SC degradati — ripristino valori sicuri")
                    pesi_caricati = {
                        'oracolo_fp': 0.25, 'signal_tracker': 0.20,
                        'campo_carica': 0.30, 'matrimonio': 0.13, 'phantom_ratio': 0.12
                    }
                if pesi_caricati.get('campo_carica', 0) > 0.45:
                    log.warning("[RUNTIME_LOAD] ⚠️ Pesi degradati — reset default")
                    pesi_caricati = dict(SuperCervello.PESI_DEFAULT)
                bot.supercervello._pesi = pesi_caricati
                log.info(f"[RUNTIME_LOAD] 🧠 Pesi SC: {pesi_caricati}")

            # Ripristina Veritas
            if 'veritas_closed' in data and hasattr(bot, 'veritas'):
                for s in data['veritas_closed']:
                    bot.veritas._closed.append(s)
                    bot.veritas._aggiorna_stats(s)
                log.info(f"[RUNTIME_LOAD] ⚖️ Veritas ripristinato: {len(data['veritas_closed'])} segnali")
            if 'veritas_stats' in data and hasattr(bot, 'veritas'):
                for k,v in data['veritas_stats'].items():
                    if k not in bot.veritas._stats:
                        bot.veritas._stats[k] = v

            # Soglia calcolata dinamicamente dal Signal Tracker — mai dal DB
            # Il DB non salva la soglia: viene ricalcolata ad ogni boot
            _soglia_dinamica = _calcola_soglia_da_signal_tracker(bot)
            bot.campo.SOGLIA_BASE = _soglia_dinamica['base']
            bot.campo.SOGLIA_MIN  = _soglia_dinamica['min']
            log.info(f"[RUNTIME_LOAD] 🎯 Soglia dinamica calcolata: "
                    f"base={_soglia_dinamica['base']} min={_soglia_dinamica['min']} "
                    f"({_soglia_dinamica['motivo']})")

            if restored:
                log.info(f"[RUNTIME_LOAD] 💾 Stato runtime ripristinato → {' | '.join(restored)}")

        except Exception as e:
            log.error(f"[RUNTIME_LOAD] {e}")

    def save_signal_tracker(self, tracker):
        """Persiste le stats del PreTradeSignalTracker su DB — sopravvive ai restart."""
        try:
            import json
            # Serializza solo _stats (le distribuzioni) — non i segnali aperti
            stats_data = {}
            for key, s in tracker._stats.items():
                stats_data[key] = {
                    'n':        s['n'],
                    'delta_30': list(s.get('delta_30', [])),
                    'delta_60': list(s.get('delta_60', [])),
                    'delta_120':list(s.get('delta_120',[])),
                    'hit_30':   list(s.get('hit_30',   [])),
                    'hit_60':   list(s.get('hit_60',   [])),
                    'hit_120':  list(s.get('hit_120',  [])),
                    'pnl_sim':  list(s.get('pnl_sim',  [])),
                }
            conn = sqlite3.connect(self.db_path)
            conn.execute("INSERT OR REPLACE INTO bot_state VALUES ('signal_tracker', ?)",
                        (json.dumps({
                            'stats':        stats_data,
                            'total_closed': len(tracker._closed),
                        }),))
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"[SIGNAL_SAVE] {e}")

    def load_signal_tracker(self, tracker):
        """Ripristina le stats del PreTradeSignalTracker dal DB."""
        try:
            import json
            conn = sqlite3.connect(self.db_path)
            rows = dict(conn.execute("SELECT key, value FROM bot_state WHERE key='signal_tracker'").fetchall())
            conn.close()
            if 'signal_tracker' not in rows:
                return
            data = json.loads(rows['signal_tracker'])
            stats = data.get('stats', {})
            for key, s in stats.items():
                tracker._stats[key] = {
                    'n':        s.get('n', 0),
                    'delta_30': s.get('delta_30', []),
                    'delta_60': s.get('delta_60', []),
                    'delta_120':s.get('delta_120',[]),
                    'hit_30':   s.get('hit_30',   []),
                    'hit_60':   s.get('hit_60',   []),
                    'hit_120':  s.get('hit_120',  []),
                    'pnl_sim':  s.get('pnl_sim',  []),
                }
            total = data.get('total_closed', 0)
            log.info(f"[SIGNAL_LOAD] 📡 SignalTracker ripristinato: "
                     f"{len(stats)} contesti, {total} segnali storici")
        except Exception as e:
            log.error(f"[SIGNAL_LOAD] {e}")

    def load_brain(self, oracolo, memoria, calibratore):
        """
        Ripristina l'intelligenza accumulata da SQLite dopo un restart.
        Il bot riprende esattamente da dove aveva lasciato.
        """
        try:
            import json
            conn  = sqlite3.connect(self.db_path)
            rows  = dict(conn.execute("SELECT key, value FROM bot_state").fetchall())
            conn.close()

            restored = []

            # -- OracoloDinamico 2.0 --------------------------------------
            if 'oracolo' in rows:
                raw = json.loads(rows['oracolo'])
                deque_fields = ['durations_win', 'durations_loss', 'rsi_win', 'rsi_loss',
                               'drift_win', 'drift_loss', 'range_pos_win', 'range_pos_loss',
                               'post_continued', 'post_delta']
                for fp, data in raw.items():
                    entry = {
                        'wins':    float(data.get('wins', 0)),
                        'samples': float(data.get('samples', 0)),
                        'pnl_sum': float(data.get('pnl_sum', 0)),
                        'real_samples': int(data.get('real_samples', 0)),
                    }
                    for df in deque_fields:
                        if df in data and isinstance(data[df], list):
                            entry[df] = deque(data[df], maxlen=50)
                        else:
                            entry[df] = deque(maxlen=50)
                    oracolo._memory[fp] = entry
                restored.append(f"Oracolo 2.0: {len(oracolo._memory)} fingerprint, "
                               f"{sum(m.get('real_samples',0) for m in oracolo._memory.values())} real")

            # -- MemoriaMatrimoni ----------------------------------------
            if 'memoria' in rows:
                md = json.loads(rows['memoria'])
                for k, v in md.get('trust', {}).items():
                    memoria.trust[k] = v
                for k, v in md.get('separazione', {}).items():
                    memoria.separazione[k] = v
                for k, v in md.get('blacklist', {}).items():
                    memoria.blacklist[k] = v
                for mat in md.get('divorzio', []):
                    memoria.divorzio.add(mat)
                for k, v in md.get('wins', {}).items():
                    memoria.wins[k] = v
                for k, v in md.get('losses', {}).items():
                    memoria.losses[k] = v
                for k, v in md.get('wr_history', {}).items():
                    memoria.wr_history[k] = list(v)
                restored.append(f"Memoria: {len(memoria.divorzio)} divorzi, "
                               f"{sum(1 for v in memoria.blacklist.values() if v > 0)} separazioni")

            # -- AutoCalibratore params -----------------------------------
            if 'calibra_params' in rows:
                saved = json.loads(rows['calibra_params'])
                calibratore.params.update(saved)
                restored.append(f"Calibra: seed={saved.get('seed_threshold', '?')}")

            if restored:
                log.info(f"[BRAIN_LOAD] 🧠 Intelligenza ripristinata → {' | '.join(restored)}")
            else:
                log.info("[BRAIN_LOAD] Primo avvio - nessuna memoria precedente")

        except Exception as e:
            log.error(f"[BRAIN_LOAD] {e} - parto da zero")

    def save(self, capital: float, total_trades: int):
        """Persiste capital e total_trades su SQLite."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("INSERT OR REPLACE INTO bot_state VALUES ('capital', ?)",      (str(capital),))
            conn.execute("INSERT OR REPLACE INTO bot_state VALUES ('total_trades', ?)", (str(total_trades),))
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"[PERSIST] Save: {e}")

# ===========================================================================
# ★ REGIME DETECTOR - contesto macro sopra tutto
#   Classifica il regime strutturale del mercato su finestra larga.
#   TRENDING_BULL / TRENDING_BEAR / RANGING / EXPLOSIVE
#   Il regime cambia i parametri di tutto il sistema sottostante.
# ===========================================================================

class RegimeDetector:
    """
    Osserva 500 tick e classifica il regime macro.
    Non si confonde con i tick singoli - lavora sulla struttura.

    Regimi:
      TRENDING_BULL  - trend rialzista strutturale, alta directional consistency
      TRENDING_BEAR  - trend ribassista strutturale
      RANGING        - mercato laterale, alta volatilita relativa, bassa direzione
      EXPLOSIVE      - breakout improvviso, volume spike + range expansion
    """

    WINDOW = 500   # tick per valutare il regime

    # Moltiplicatori per ogni regime - applicati ai parametri del calibratore
    REGIME_PARAMS = {
        'TRENDING_BULL': {
            'seed_mult':      0.90,   # leggermente più permissivo
            'fp_wr_mult':     0.95,   # accetta contesti leggermente meno perfetti
            'size_mult':      1.25,   # size più grande in trend
            'drawdown_mult':  1.20,   # tollera più drawdown in trend
        },
        'TRENDING_BEAR': {
            'seed_mult':      1.20,   # più selettivo
            'fp_wr_mult':     1.10,
            'size_mult':      0.70,   # size ridotta
            'drawdown_mult':  0.80,   # meno tolleranza
        },
        'RANGING': {
            'seed_mult':      1.30,   # molto selettivo - il ranging è il nemico
            'fp_wr_mult':     1.15,
            'size_mult':      0.60,
            'drawdown_mult':  0.70,
        },
        'EXPLOSIVE': {
            'seed_mult':      0.85,   # velocita conta - entra prima
            'fp_wr_mult':     0.90,
            'size_mult':      1.50,   # massima size in breakout
            'drawdown_mult':  1.50,   # lascia correre
        },
    }

    def __init__(self):
        self.prices    = deque(maxlen=self.WINDOW)
        self.volumes   = deque(maxlen=self.WINDOW)
        self._regime   = 'RANGING'   # default conservativo
        self._confidence = 0.0

    def add_tick(self, price: float, volume: float = 1.0):
        self.prices.append(price)
        self.volumes.append(volume)

    def detect(self) -> tuple:
        """
        Ritorna (regime: str, confidence: float, dettaglio: dict)
        """
        if len(self.prices) < 100:
            return 'RANGING', 0.0, {}

        prices  = list(self.prices)
        volumes = list(self.volumes)
        n       = len(prices)

        # -- Trend strutturale ---------------------------------------------
        # Regressione lineare semplificata: confronta meta iniziale vs finale
        mid        = n // 2
        avg_first  = sum(prices[:mid]) / mid
        avg_second = sum(prices[mid:]) / (n - mid)
        trend_pct  = (avg_second - avg_first) / avg_first * 100

        # -- Directional Consistency su finestra larga ---------------------
        changes    = [prices[i+1] - prices[i] for i in range(n-1)]
        up_count   = sum(1 for c in changes if c > 0)
        dir_ratio  = up_count / len(changes)   # 0=tutto giù, 1=tutto su

        # -- Volatilita strutturale -----------------------------------------
        abs_changes = [abs(c) for c in changes]
        avg_change  = sum(abs_changes) / len(abs_changes)
        # Confronta volatilita prima vs seconda meta
        vol_first   = sum(abs_changes[:mid]) / mid
        vol_second  = sum(abs_changes[mid:]) / (n - mid)
        vol_ratio   = vol_second / max(vol_first, 0.001)

        # -- Volume acceleration --------------------------------------------
        vol_recent  = sum(volumes[-50:]) / 50
        vol_base    = sum(volumes[:50])  / 50
        vol_accel   = vol_recent / max(vol_base, 0.001)

        # -- Classificazione -----------------------------------------------
        regime     = 'RANGING'
        confidence = 0.5

        if vol_accel > 2.0 and vol_ratio > 1.5:
            # Volume esploso + volatilita in aumento → EXPLOSIVE
            regime     = 'EXPLOSIVE'
            confidence = min(1.0, vol_accel / 3.0)

        elif trend_pct > 0.5 and dir_ratio > 0.55:
            # Trend rialzista strutturale
            regime     = 'TRENDING_BULL'
            confidence = min(1.0, (dir_ratio - 0.5) * 4)

        elif trend_pct < -0.5 and dir_ratio < 0.45:
            # Trend ribassista strutturale
            regime     = 'TRENDING_BEAR'
            confidence = min(1.0, (0.5 - dir_ratio) * 4)

        else:
            # Laterale
            regime     = 'RANGING'
            confidence = min(1.0, 1.0 - abs(dir_ratio - 0.5) * 4)

        self._regime     = regime
        self._confidence = confidence

        return regime, confidence, {
            'trend_pct':  round(trend_pct, 3),
            'dir_ratio':  round(dir_ratio, 3),
            'vol_accel':  round(vol_accel, 3),
            'vol_ratio':  round(vol_ratio, 3),
        }

    @property
    def regime(self) -> str:
        return self._regime

    def get_multipliers(self) -> dict:
        return self.REGIME_PARAMS.get(self._regime, self.REGIME_PARAMS['RANGING'])


# ===========================================================================
# ★ MOMENTUM DECELEROMETER - exit intelligente
#   Non misura il momentum - misura quanto velocemente sta decelerando.
#   Uscire quando decelera forte, non quando è gia morto.
# ===========================================================================

class MomentumDecelerometer:
    """
    Calcola la derivata seconda del momentum.
    Se il momentum stava salendo e ora sta scendendo velocemente
    → segnale di uscita anticipata prima che il prezzo inverta.

    Restituisce:
      decel_score [0-1] - 0=momentum stabile, 1=decelera forte
      should_exit bool  - True se la decelerazione supera la soglia
    """

    WINDOW_FAST = 5    # tick per momentum veloce
    WINDOW_SLOW = 15   # tick per momentum lento
    DECEL_THRESHOLD = 0.65   # oltre questa soglia → esci

    def __init__(self):
        self.prices = deque(maxlen=50)

    def add_price(self, price: float):
        self.prices.append(price)

    def analyze(self) -> dict:
        if len(self.prices) < self.WINDOW_SLOW + 5:
            return {'decel_score': 0.0, 'should_exit': False}

        prices = list(self.prices)

        # Momentum veloce: variazione media negli ultimi WINDOW_FAST tick
        fast_changes = [prices[i+1] - prices[i]
                        for i in range(len(prices)-self.WINDOW_FAST, len(prices)-1)]
        mom_fast = sum(fast_changes) / len(fast_changes) if fast_changes else 0

        # Momentum lento: variazione media negli ultimi WINDOW_SLOW tick
        slow_start = len(prices) - self.WINDOW_SLOW
        slow_changes = [prices[i+1] - prices[i]
                        for i in range(slow_start, len(prices)-1)]
        mom_slow = sum(slow_changes) / len(slow_changes) if slow_changes else 0

        # Decelerazione: il momentum veloce è molto più basso di quello lento
        # (il trend sta perdendo forza)
        if abs(mom_slow) < 0.001:
            decel_score = 0.0
        else:
            # Se mom_fast < mom_slow → decelera (in trade long)
            decel = (mom_slow - mom_fast) / abs(mom_slow)
            decel_score = max(0.0, min(1.0, decel))

        return {
            'decel_score': round(decel_score, 4),
            'mom_fast':    round(mom_fast, 4),
            'mom_slow':    round(mom_slow, 4),
            'should_exit': decel_score > self.DECEL_THRESHOLD,
        }


# ===========================================================================
# ★ POSITION SIZER - la tua fisica applicata
#   Size come funzione CONTINUA dell'intensita dell'impulso.
#   Non più 1.0 / 1.3 / 1.5 discreti - una curva che riflette
#   esattamente quanto il mercato ti sta dando.
# ===========================================================================

class PositionSizer:
    """
    Calcola la size ottimale come funzione continua di 3 segnali:
      1. seed_score      - forza dell'impulso (peso 40%)
      2. fingerprint_wr  - affidabilita storica del contesto (peso 35%)
      3. confidence      - certezza del matrimonio (peso 25%)

    Poi applica il moltiplicatore di regime.

    Output: size_factor [0.5 – 2.0]
    Dove 1.0 = size base, 2.0 = massimo, 0.5 = minimo di sicurezza
    """

    W_SEED   = 0.40
    W_FP_WR  = 0.35
    W_CONF   = 0.25

    SIZE_MIN = 0.5
    SIZE_MAX = 2.0

    def calculate(self, seed_score: float, fingerprint_wr: float,
                  confidence: float, regime_mult: float = 1.0) -> dict:
        """
        Ritorna {'size_factor': float, 'breakdown': dict}
        """
        # Normalizza ogni componente in [0, 1]
        # seed_score è gia [0, 1]
        seed_norm = min(1.0, max(0.0, seed_score))

        # fingerprint_wr [0.30, 0.80] → [0, 1] (calibrato su valori reali)
        fp_norm = min(1.0, max(0.0, (fingerprint_wr - 0.30) / 0.50))

        # confidence [0.05, 0.95] → [0, 1]
        conf_norm = min(1.0, max(0.0, (confidence - 0.05) / 0.90))

        # Score composito
        score = (seed_norm   * self.W_SEED  +
                 fp_norm     * self.W_FP_WR +
                 conf_norm   * self.W_CONF)

        # Mappa da [0,1] a [SIZE_MIN, SIZE_MAX] con curva non lineare
        # Le posizioni forti crescono più che proporzionalmente
        size_raw = self.SIZE_MIN + (self.SIZE_MAX - self.SIZE_MIN) * (score ** 1.5)

        # Applica moltiplicatore regime
        size_final = min(self.SIZE_MAX, max(self.SIZE_MIN, size_raw * regime_mult))

        return {
            'size_factor': round(size_final, 3),
            'score':       round(score, 3),
            'seed_norm':   round(seed_norm, 3),
            'fp_norm':     round(fp_norm, 3),
            'conf_norm':   round(conf_norm, 3),
        }


# ===========================================================================
# ★ AUTO CALIBRATORE - TUA INVENZIONE
#   Osserva i risultati reali e aggiusta i parametri statici.
#   Stessa pazienza e cautela del DNA del sistema:
#   - Minimo 30 trade prima di toccare qualsiasi soglia
#   - Step massimo ±0.02 per aggiustamento
#   - Invertibile se la modifica peggiora i risultati
#   - Log narrativo di ogni modifica
# ===========================================================================

class AutoCalibratore:
    """
    Calibra automaticamente i parametri statici basandosi sui risultati reali.
    Non è ubriaco: aspetta evidenza solida, cambia in piccoli passi,
    ricorda ogni modifica e può tornare indietro.
    """

    # -- Limiti di sicurezza - non si esce mai da questi range -------------
    LIMITS = {
        'seed_threshold':      (0.25, 0.70),   # mai troppo permissivo né troppo restrittivo
        'cap1_soglia_buona':   (0.45, 0.80),   # Capsule1 soglia "coerenza buona"
        'cap1_soglia_perfetta':(0.60, 0.90),   # Capsule1 soglia "coerenza perfetta"
        'cap3_fp_minimo':      (0.35, 0.65),   # Capsule3 protezione fp minimo
        'cap4_soglia_buona':   (0.50, 0.80),   # Capsule4 opportunita buona
        'cap5_conf_ok':        (0.50, 0.80),   # Capsule5 timing OK
        'divorce_drawdown':    (1.5,  5.0),    # drawdown trigger
    }

    STEP          = 0.05    # era 0.02 - troppo lento, il mercato cambia regime in minuti
    MIN_TRADES    = 10      # era 30 - con stop loss 2% il rischio è controllato, impara prima
    MIN_DELTA_WR  = 0.05    # differenza minima WR reale vs atteso per intervenire
    HISTORY_SIZE  = 5       # quante calibrazioni ricordare per inversione
    MIN_CALIB_INTERVAL = 900  # minimo 15 minuti tra calibrazioni - anti-oscillazione

    def __init__(self):
        # Parametri correnti - inizializzati ai valori di default
        self.params = {
            'seed_threshold':       SEED_ENTRY_THRESHOLD,
            'cap1_soglia_buona':    0.60,
            'cap1_soglia_perfetta': 0.75,
            'cap3_fp_minimo':       0.55,
            'cap4_soglia_buona':    0.65,
            'cap5_conf_ok':         0.65,
            'divorce_drawdown':     DIVORCE_DRAWDOWN_PCT,
        }
        # Storico per inversione: {param: [(valore_prima, valore_dopo, wr_al_momento)]}
        self._history: dict = {k: [] for k in self.params}
        # Osservazioni per calibrazione: lista di (seed_score, wr_contesto, is_win)
        self._obs: list = []
        self._calibrazioni_log: list = []   # log narrativo
        self._last_calib_time: float = 0    # rate limit anti-oscillazione

    def registra_osservazione(self, seed_score: float, fingerprint_wr: float,
                               is_win: bool, divorce_drawdown_usato: float):
        """Chiamato dopo ogni trade chiuso."""
        self._obs.append({
            'seed_score':     seed_score,
            'fingerprint_wr': fingerprint_wr,
            'is_win':         is_win,
            'drawdown':       divorce_drawdown_usato,
        })

    def calibra(self) -> dict:
        """
        Analizza le osservazioni accumulate.
        Se ci sono evidenze solide (≥ MIN_TRADES) aggiusta i parametri.
        Rate limit: massimo 1 calibrazione ogni MIN_CALIB_INTERVAL secondi.
        Ritorna dict con parametri aggiornati e log delle modifiche.
        """
        if len(self._obs) < self.MIN_TRADES:
            return {}   # troppo poco per giudicare

        # Rate limit: non calibrare troppo spesso
        now = time.time()
        if now - self._last_calib_time < self.MIN_CALIB_INTERVAL:
            return {}
        self._last_calib_time = now

        modifiche = {}
        n = len(self._obs)
        wins = sum(1 for o in self._obs if o['is_win'])
        wr_reale = wins / n

        # -- 1. SEED THRESHOLD ---------------------------------------------
        # Se la maggior parte dei trade ha seed_score vicino alla soglia attuale
        # e WR è basso → alza la soglia (sii più selettivo)
        # Se WR è alto ma entri raramente → abbassa leggermente
        seed_scores = [o['seed_score'] for o in self._obs]
        avg_seed = sum(seed_scores) / len(seed_scores)
        current_seed = self.params['seed_threshold']

        if wr_reale < 0.45 and avg_seed < current_seed + 0.10:
            # WR basso e i trade hanno seed basso → soglia troppo permissiva
            new_val = min(current_seed + self.STEP,
                         self.LIMITS['seed_threshold'][1])
            if new_val != current_seed:
                self._aggiusta('seed_threshold', new_val, wr_reale,
                    f"WR={wr_reale:.0%} basso su {n} trade, avg_seed={avg_seed:.3f} → alzo soglia")
                modifiche['seed_threshold'] = new_val

        elif wr_reale > 0.70 and n > self.MIN_TRADES * 2:
            # WR molto alto → possiamo essere leggermente meno restrittivi
            new_val = max(current_seed - self.STEP,
                         self.LIMITS['seed_threshold'][0])
            if new_val != current_seed:
                self._aggiusta('seed_threshold', new_val, wr_reale,
                    f"WR={wr_reale:.0%} eccellente su {n} trade → abbasso soglia leggermente")
                modifiche['seed_threshold'] = new_val

        # -- 2. DIVORCE DRAWDOWN -------------------------------------------
        # Se molti trade escono per TIMEOUT (non per divorce) con drawdown alto
        # → il drawdown trigger è troppo permissivo, abbassalo
        drawdowns = [o['drawdown'] for o in self._obs if o['drawdown'] > 0]
        if drawdowns:
            avg_dd = sum(drawdowns) / len(drawdowns)
            current_dd = self.params['divorce_drawdown']
            if avg_dd > current_dd * 0.8 and wr_reale < 0.50:
                new_val = max(current_dd - self.STEP * 5,
                             self.LIMITS['divorce_drawdown'][0])
                if new_val != current_dd:
                    self._aggiusta('divorce_drawdown', new_val, wr_reale,
                        f"avg_drawdown={avg_dd:.1f}% vicino alla soglia, WR basso → stringo drawdown")
                    modifiche['divorce_drawdown'] = new_val

        # -- 3. CAP1 SOGLIA COERENZA ---------------------------------------
        # Osserva quanti trade hanno fingerprint_wr nel range "buono" (0.60-0.75)
        # Se quelli perdono → alza la soglia di ingresso coerenza
        fp_buono = [o for o in self._obs
                    if 0.60 <= o['fingerprint_wr'] < 0.75]
        if len(fp_buono) >= 10:
            wr_fp_buono = sum(1 for o in fp_buono if o['is_win']) / len(fp_buono)
            current_c1 = self.params['cap1_soglia_buona']
            if wr_fp_buono < 0.45:
                new_val = min(current_c1 + self.STEP,
                             self.LIMITS['cap1_soglia_buona'][1])
                if new_val != current_c1:
                    self._aggiusta('cap1_soglia_buona', new_val, wr_fp_buono,
                        f"Trade fp_wr 0.60-0.75 hanno WR={wr_fp_buono:.0%} → alzo soglia coerenza")
                    modifiche['cap1_soglia_buona'] = new_val

        # Reset osservazioni dopo calibrazione (mantieni le ultime MIN_TRADES/2)
        self._obs = self._obs[-(self.MIN_TRADES // 2):]

        return modifiche

    def _aggiusta(self, param: str, new_val: float, wr_al_momento: float, motivo: str):
        """Applica il cambio, lo registra per eventuale inversione."""
        old_val = self.params[param]
        self.params[param] = new_val

        # Storico per inversione
        self._history[param].append((old_val, new_val, wr_al_momento))
        if len(self._history[param]) > self.HISTORY_SIZE:
            self._history[param].pop(0)

        msg = f"[CALIBRA] 🎯 {param}: {old_val:.3f} → {new_val:.3f} | {motivo}"
        self._calibrazioni_log.append({
            'ts':    datetime.utcnow().isoformat(),
            'param': param,
            'from':  old_val,
            'to':    new_val,
            'why':   motivo,
        })
        log.info(msg)

    def inverti_se_peggiorato(self, wr_attuale: float):
        """
        Se dopo una calibrazione il WR è peggiorato, torna al valore precedente.
        Chiamato ogni 20 trade dopo una modifica.
        """
        for param, history in self._history.items():
            if not history:
                continue
            old_val, new_val, wr_prima = history[-1]
            if wr_attuale < wr_prima - self.MIN_DELTA_WR:
                # La modifica ha peggiorato le cose → torna indietro
                self.params[param] = old_val
                history.pop()
                log.warning(f"[CALIBRA] ↩️  INVERSIONE {param}: {new_val:.3f} → {old_val:.3f} "
                           f"(WR prima={wr_prima:.0%} ora={wr_attuale:.0%})")

    def get_params(self) -> dict:
        return dict(self.params)

    def get_log(self) -> list:
        return list(self._calibrazioni_log[-10:])   # ultimi 10 eventi


# ===========================================================================
# ★ CAMPO GRAVITAZIONALE - MOTORE 2 (CARTESIANO)
#   Nessun filtro binario tranne i veti assoluti.
#   Ogni condizione accumula punti. La soglia si muove con il contesto.
#   La size è funzione continua della distanza punteggio-soglia.
# ===========================================================================

class CampoGravitazionale:
    """
    Entry engine cartesiano: ogni dimensione contribuisce punti,
    la soglia è dinamica, la size è continua.

    Veti assoluti (non negoziabili):
      - TRAP / PANIC (combinazioni tossiche provate)
      - DIVORZIO PERMANENTE
      - FANTASMA con evidenza forte (samples>20, WR<30%)
      - 3+ loss consecutivi

    Tutto il resto → punteggio 0-100 vs soglia dinamica 35-90.
    """

    # -- VETI ASSOLUTI -----------------------------------------------------
    # LONG: non entrare in mercato che crolla
    VETI_LONG = {
        ("DEBOLE", "ALTA", "DOWN"),    # TRAP - WR 5% per LONG
        ("FORTE",  "ALTA", "DOWN"),    # PANIC - WR 15% per LONG
        ("DEBOLE", "ALTA", "SIDEWAYS"),# RANGE_VOL_W - WR 19% dati reali
        ("FORTE",  "ALTA", "SIDEWAYS"),# RANGE_VOL_F - WR 34% dati reali Oracolo
        ("MEDIO",  "ALTA", "SIDEWAYS"),# RANGE_VOL_M - WR 28% dati reali Oracolo
    }
    # SHORT: non entrare in mercato che esplode al rialzo
    VETI_SHORT = {
        ("FORTE",  "BASSA", "UP"),     # STRONG_BULL - WR 5% per SHORT
        ("FORTE",  "MEDIA", "UP"),     # STRONG_MED - pericoloso per SHORT
        ("DEBOLE", "ALTA", "SIDEWAYS"),# RANGE_VOL_W - WR 10% in SHORT
        ("FORTE",  "ALTA", "SIDEWAYS"),# RANGE_VOL_F - WR 12% in SHORT
        ("MEDIO",  "ALTA", "SIDEWAYS"),# RANGE_VOL_M - WR 8% in SHORT
    }
    FANTASMA_VETO_MIN_SAMPLES = 20
    FANTASMA_VETO_MAX_WR      = 0.30
    MAX_LOSS_CONSECUTIVI      = 3

    # -- PESI DEL CAMPO (totale = 100) -------------------------------------
    # V2: aggiunto RSI e MACD come consiglieri. Pesi ridistribuiti.
    W_SEED        = 25    # era 30 - cede 5 ai consiglieri
    W_FINGERPRINT = 20    # era 25 - cede 5 ai consiglieri
    W_MOMENTUM    = 12    # era 15
    W_TREND       = 12    # era 15
    W_VOLATILITY  = 8     # era 10
    W_REGIME      = 3     # era 5
    W_RSI         = 10    # NUOVO - il consigliere ipervenduto/ipercomprato
    W_MACD        = 10    # NUOVO - il consigliere trend/momentum

    # -- SCORING PER DIMENSIONE --------------------------------------------
    # LONG - impulso rialzista
    MOMENTUM_SCORE_LONG  = {"FORTE": 1.0,  "MEDIO": 0.67, "DEBOLE": 0.20}
    TREND_SCORE_LONG     = {"UP": 1.0,     "SIDEWAYS": 0.47, "DOWN": 0.0}
    REGIME_SCORE_LONG    = {"TRENDING_BULL": 1.0, "EXPLOSIVE": 0.80,
                            "RANGING": 0.20, "TRENDING_BEAR": 0.0}

    # SHORT - impulso ribassista (tutto invertito)
    MOMENTUM_SCORE_SHORT = {"FORTE": 0.20, "MEDIO": 0.67, "DEBOLE": 1.0}
    TREND_SCORE_SHORT    = {"UP": 0.0,     "SIDEWAYS": 0.47, "DOWN": 1.0}
    REGIME_SCORE_SHORT   = {"TRENDING_BULL": 0.0, "EXPLOSIVE": 0.80,
                            "RANGING": 0.20, "TRENDING_BEAR": 1.0}

    # VOL_SCORE è uguale per LONG e SHORT - alta volatilita è sempre rischio
    VOL_SCORE       = {"BASSA": 1.0,  "MEDIA": 0.60, "ALTA": 0.20}

    # -- SOGLIA DINAMICA ---------------------------------------------------
    SOGLIA_BASE = 52
    REGIME_FACTOR = {"TRENDING_BULL": 0.80, "EXPLOSIVE": 0.85,
                     "RANGING": 1.20, "TRENDING_BEAR": 1.10}
    # RANGING: era 1.10, ora 1.00 - soglia formula 75.9 irraggiungibile, score max realistico 64
    # Con 1.00: soglia RANGING+ALTA = 60 × 1.00 × 1.05 = 63.0 (raggiungibile)
    VOL_FACTOR    = {"BASSA": 0.90, "MEDIA": 1.0, "ALTA": 1.00}
    # ALTA: era 1.05, ora 1.00 - phantom SCORE_INSUFF WR 65% R/R 2.04, profittevoli
    # Soglia RANGING+ALTA: 60 × 1.00 × 1.00 = 60.0 (trade score 58-63 passano)
    SOGLIA_MIN    = 48    # PAVIMENTO calibrato su dati reali Signal Tracker
    SOGLIA_MAX    = 80    # era 90 - phantom SCORE_INSUFFICIENTE dice -$3871, troppo alto in RANGING

    # -- SIZE CONTINUA -----------------------------------------------------
    SIZE_MIN = 0.5
    SIZE_MAX = 2.0

    # -- DRIFT VETO -----------------------------------------------------
    DRIFT_VETO_THRESHOLD = -0.20   # era -0.10 - phantom WR 81% bloccati, sta bloccando i migliori

    # -- WARMUP ---------------------------------------------------------
    WARMUP_TICKS = 200   # tick minimi prima di operare - buffer devono riempirsi

    def __init__(self):
        self._recent_results = deque(maxlen=20)
        self._tick_count = 0   # conta tick dal boot
        self._direction = "LONG"  # LONG o SHORT - il bridge decide
        self._direction_last_change = 0       # timestamp ultimo flip
        self._direction_bearish_streak = 0    # tick consecutivi bearish >=2
        # -- PRE-BREAKOUT DETECTOR -----------------------------------------
        self._prices_short = deque(maxlen=50)     # ultimi 50 prezzi per compressione
        self._seed_history = deque(maxlen=10)     # ultimi 10 seed per derivata
        self._volumes_short = deque(maxlen=50)    # ultimi 50 volumi per accelerazione
        # -- DRIFT DETECTOR ------------------------------------------------
        self._prices_long = deque(maxlen=200)     # ultimi 200 prezzi per drift
        # -- RSI + MACD CONSIGLIERI ----------------------------------------
        self._prices_ta = deque(maxlen=200)       # buffer prezzi CAMPIONATI per indicatori tecnici
        self._ta_tick_counter = 0                  # conta tick per campionamento
        self._ta_sample_rate = 50                  # campiona ogni 50 tick (non ogni tick!)
        self._rsi_period = 14                     # RSI standard 14 periodi
        self._macd_fast = 12                      # MACD EMA veloce
        self._macd_slow = 26                      # MACD EMA lenta
        self._macd_signal = 9                     # MACD signal line
        self._last_rsi = 50.0                     # RSI corrente
        self._last_macd = 0.0                     # MACD line corrente
        self._last_macd_signal = 0.0              # MACD signal corrente
        self._last_macd_hist = 0.0                # MACD histogram
        # -- PREBREAKOUT AUTO-TUNING (META-REGOLA) -------------------------
        self._pb3_results = deque(maxlen=20)       # ultimi 20 trade con pb=3/3: (is_win, exit_reason, pnl)
        self._pb3_compression_threshold = 0.0003   # si stringe se WR pb3 < 50%
        self._pb3_vol_acc_threshold = 1.3           # si stringe se troppi falsi

    def feed_tick(self, price: float, volume: float, seed_score: float):
        """Alimenta tutti i detector con dati tick-by-tick."""
        self._prices_short.append(price)
        self._volumes_short.append(volume)
        self._seed_history.append(seed_score)
        self._prices_long.append(price)
        self._tick_count += 1

        # -- CAMPIONA per RSI/MACD ogni 50 tick ------------------------
        # I tick sono troppo veloci - RSI su tick-by-tick va a 100/0.
        # Campionando ogni 50 tick creiamo "candele" virtuali stabili.
        self._ta_tick_counter += 1
        if self._ta_tick_counter >= self._ta_sample_rate:
            self._ta_tick_counter = 0
            self._prices_ta.append(price)
            if len(self._prices_ta) >= 30:
                self._update_rsi()
                self._update_macd()

    def score_now(self, seed_score: float, fingerprint_wr: float,
                  momentum: str, volatility: str, trend: str,
                  regime: str, direction: str = "LONG") -> dict:
        """
        Calcola score e soglia ORA senza decidere nulla.
        Nessun veto, nessun effetto collaterale — pura osservazione.
        Chiamato ogni tick per il SignalTracker.
        """
        if self._tick_count < 200:
            return {'score': 0, 'soglia': 60, 'valid': False}

        W = {"seed":25,"fp":20,"mom":12,"trend":12,"vol":8,"regime":3,"rsi":10,"mac":10}
        MOM_L  = {"FORTE":1.0,"MEDIO":0.67,"DEBOLE":0.20}
        MOM_S  = {"FORTE":0.20,"MEDIO":0.67,"DEBOLE":1.0}
        TRD_L  = {"UP":1.0,"SIDEWAYS":0.47,"DOWN":0.0}
        TRD_S  = {"UP":0.0,"SIDEWAYS":0.47,"DOWN":1.0}
        REG_L  = {"TRENDING_BULL":1.0,"EXPLOSIVE":0.80,"RANGING":0.20,"TRENDING_BEAR":0.0}
        REG_S  = {"TRENDING_BULL":0.0,"EXPLOSIVE":0.80,"RANGING":0.20,"TRENDING_BEAR":1.0}
        VOL_S  = {"BASSA":1.0,"MEDIA":0.60,"ALTA":0.20}
        REG_F  = {"TRENDING_BULL":0.80,"EXPLOSIVE":0.85,"RANGING":1.00,"TRENDING_BEAR":1.10}

        if direction == "SHORT":
            s_mom = MOM_S.get(momentum,0.5)*W["mom"]
            s_trd = TRD_S.get(trend,0.5)*W["trend"]
            s_reg = REG_S.get(regime,0.2)*W["regime"]
        else:
            s_mom = MOM_L.get(momentum,0.5)*W["mom"]
            s_trd = TRD_L.get(trend,0.5)*W["trend"]
            s_reg = REG_L.get(regime,0.2)*W["regime"]

        s_seed = min(1.0,max(0.0,(seed_score-0.20)/0.60))*W["seed"]
        s_fp   = min(1.0,max(0.0,(fingerprint_wr-0.30)/0.50))*W["fp"]
        s_vol  = VOL_S.get(volatility,0.5)*W["vol"]
        s_rsi  = self._rsi_score()*W["rsi"]
        s_macd = self._macd_score()*W["mac"]
        score  = s_seed+s_fp+s_mom+s_trd+s_vol+s_reg+s_rsi+s_macd

        # Score max per context_ratio
        sm = (W["seed"]+W["fp"]+
              (MOM_S if direction=="SHORT" else MOM_L).get(momentum,0.5)*W["mom"]+
              (TRD_S if direction=="SHORT" else TRD_L).get(trend,0.5)*W["trend"]+
              VOL_S.get(volatility,0.5)*W["vol"]+
              (REG_S if direction=="SHORT" else REG_L).get(regime,0.2)*W["regime"]+
              W["rsi"]+W["mac"])
        ctx   = sm/100.0
        rf    = REG_F.get(regime,1.0)
        soglia_raw = 60*ctx*rf
        soglia = max(max(48,58*ctx), min(80,soglia_raw))

        return {
            'score':  round(score,1),
            'soglia': round(soglia,1),
            'valid':  True,
            'ctx':    round(ctx,2),
        }

    def evaluate(self, seed_score, fingerprint_wr, momentum, volatility,
                 trend, regime, matrimonio_name, divorzio_set,
                 fantasma_info, loss_consecutivi, direction="LONG", **kwargs) -> dict:
        """
        Ritorna:
          enter:     bool
          score:     float (0-100)
          soglia:    float (58-80, dinamica)
          size:      float (0.5-2.0 se enter, 0.0 se no)
          veto:      str o None
          direction: "LONG" o "SHORT"
          breakdown: dict dettaglio per log
        """
        # -- VETI ASSOLUTI — ora gestiti da CapsuleManager ----------------
        # Se CapsuleManager disponibile: i veti sono nel DB, asset-aware,
        # modificabili da dashboard senza deploy.
        # Se non disponibile: fallback ai VETI_LONG/SHORT hardcodati.
        combo = (momentum, volatility, trend)
        _bot = getattr(self, '_bot_ref', None)
        _cm  = getattr(_bot, 'capsule_manager', None) if _bot else None
        if _cm is not None:
            _veto_ctx = {
                'momentum':   momentum,
                'volatility': volatility,
                'trend':      trend,
                'direction':  self._direction,
                'regime':     getattr(self, '_regime_current', ''),
                'drift_pct':  getattr(self, '_last_drift', 0.0),
            }
            _cm_result = _cm.valuta(_veto_ctx)
            if _cm_result.get('blocca'):
                return self._veto(_cm_result.get('reason', f"CM_TOSSICO_{self._direction}_{momentum}_{volatility}_{trend}"))
        else:
            # Fallback hardcodato
            veti = self.VETI_SHORT if self._direction == "SHORT" else self.VETI_LONG
            if combo in veti:
                return self._veto(f"TOSSICO_{self._direction}_{momentum}_{volatility}_{trend}")

        if matrimonio_name in divorzio_set:
            return self._veto("DIVORZIO_PERMANENTE")

        is_fantasma, fantasma_reason = fantasma_info
        if is_fantasma:
            # Solo se evidenza forte - non blocchiamo su 5 campioni
            # Il campo gia penalizza fingerprint_wr basso nel punteggio
            fp_samples = fantasma_reason  # passato come samples count
            if isinstance(fp_samples, str):
                # fantasma_info ritorna (bool, str_reason) - usiamo l'info dell'oracolo
                pass  # non è un veto forte, il punteggio basso basta

        if loss_consecutivi >= self.MAX_LOSS_CONSECUTIVI:
            # Soglia sale, non veto assoluto. Trade forti passano ancora.
            pass  # gestito sotto nel calcolo soglia come loss_f

        # -- WARMUP INTELLIGENTE - la volpe non entra cieca ------------
        # Non basta contare i tick. Ogni senso deve essere attivo:
        #   - Tick >= 200 (buffer base)
        #   - prices_long >= 100 (drift affidabile)
        #   - prices_ta >= 35 (RSI=14 periodi + MACD=26+9=35 periodi)
        # ~6 minuti di warmup - la volpe annusa, guarda, ascolta.
        warmup_checks = []
        if self._tick_count < 200:
            warmup_checks.append(f"tick={self._tick_count}/200")
        if len(self._prices_long) < 100:
            warmup_checks.append(f"drift={len(self._prices_long)}/100")
        if len(self._prices_ta) < 35:
            warmup_checks.append(f"RSI_MACD={len(self._prices_ta)}/35")
        if warmup_checks:
            return self._veto(f"WARMUP_{'|'.join(warmup_checks)}")

        # -- DRIFT VETO CONTESTUALE: dipende dal regime, non fisso --------
        # RANGING: oscillazione normale, soglia larga (-0.30%)
        # TRENDING_BULL/BEAR: segnale vero, soglia stretta (-0.10%)
        # EXPLOSIVE: movimento rapido, soglia media (-0.18%)
        if len(self._prices_long) >= 100:
            _prices = list(self._prices_long)
            _avg_old = sum(_prices[:50]) / 50
            _avg_new = sum(_prices[-50:]) / 50
            _drift = (_avg_new - _avg_old) / _avg_old * 100
            _drift_thr = {"RANGING":-0.30,"TRENDING_BULL":-0.10,
                          "TRENDING_BEAR":-0.10,"EXPLOSIVE":-0.18}.get(regime,-0.20)
            if self._direction == "LONG" and _drift < _drift_thr:
                return self._veto(f"DRIFT_VETO_LONG_{_drift:+.3f}%(thr={_drift_thr})")
            elif self._direction == "SHORT" and _drift > abs(_drift_thr):
                return self._veto(f"DRIFT_VETO_SHORT_{_drift:+.3f}%(thr={_drift_thr})")

        # -- CALCOLO PUNTEGGIO CAMPO ---------------------------------------
        # Seed: normalizza [0.3, 1.0] → [0, 1]
        # Normalizzazione calibrata sui valori reali di produzione [0.20, 0.80]
        # I valori teorici [0.30, 1.0] escludevano quasi tutti i segnali reali
        s_seed = min(1.0, max(0.0, (seed_score - 0.20) / 0.60)) * self.W_SEED

        # Fingerprint WR: normalizza [0.30, 0.80] → [0, 1]
        s_fp = min(1.0, max(0.0, (fingerprint_wr - 0.30) / 0.50)) * self.W_FINGERPRINT
        self._last_fp_score = round(s_fp, 2)  # cached per heartbeat

        # Dimensioni categoriche - INVERTITE per SHORT
        if self._direction == "SHORT":
            s_mom   = self.MOMENTUM_SCORE_SHORT.get(momentum, 0.5)  * self.W_MOMENTUM
            s_trend = self.TREND_SCORE_SHORT.get(trend, 0.5)         * self.W_TREND
            s_reg   = self.REGIME_SCORE_SHORT.get(regime, 0.2)        * self.W_REGIME
        else:
            s_mom   = self.MOMENTUM_SCORE_LONG.get(momentum, 0.5)   * self.W_MOMENTUM
            s_trend = self.TREND_SCORE_LONG.get(trend, 0.5)          * self.W_TREND
            s_reg   = self.REGIME_SCORE_LONG.get(regime, 0.2)         * self.W_REGIME
        s_vol   = self.VOL_SCORE.get(volatility, 0.5)                * self.W_VOLATILITY

        # -- CONSIGLIERI TECNICI - invertiti per SHORT --------------------
        s_rsi   = self._rsi_score()                          * self.W_RSI
        s_macd  = self._macd_score()                         * self.W_MACD

        score = s_seed + s_fp + s_mom + s_trend + s_vol + s_reg + s_rsi + s_macd

        # -- SOGLIA PROPORZIONALE AL CONTESTO -----------------------------
        # La soglia scala con lo score MASSIMO raggiungibile nel contesto.
        # In TRENDING_BULL+BASSA+UP: score_max=100, soglia=60 → chiedi 60%
        # In RANGING+ALTA+SIDEWAYS:  score_max=65,  soglia=39 → chiedi 60%
        # -----------------------------------------------------------------
        if self._direction == "SHORT":
            _ctx_mom   = self.MOMENTUM_SCORE_SHORT.get(momentum, 0.5)
            _ctx_trend = self.TREND_SCORE_SHORT.get(trend, 0.5)
            _ctx_reg   = self.REGIME_SCORE_SHORT.get(regime, 0.2)
        else:
            _ctx_mom   = self.MOMENTUM_SCORE_LONG.get(momentum, 0.5)
            _ctx_trend = self.TREND_SCORE_LONG.get(trend, 0.5)
            _ctx_reg   = self.REGIME_SCORE_LONG.get(regime, 0.2)
        _ctx_vol = self.VOL_SCORE.get(volatility, 0.5)

        score_max = (1.0 * self.W_SEED + 1.0 * self.W_FINGERPRINT +
                     _ctx_mom * self.W_MOMENTUM + _ctx_trend * self.W_TREND +
                     _ctx_vol * self.W_VOLATILITY + _ctx_reg * self.W_REGIME +
                     1.0 * self.W_RSI + 1.0 * self.W_MACD)
        context_ratio = score_max / 100.0

        regime_f  = self.REGIME_FACTOR.get(regime, 1.0)
        vol_f     = self.VOL_FACTOR.get(volatility, 1.0)
        history_f = self._history_factor()
        prebreak_f, prebreak_detail, prebreak_signals = self._pre_breakout_factor()
        self._last_regime_for_drift = regime  # passa il regime al drift_factor
        drift_f, drift_detail = self._drift_factor()

        # Loss streak: alza soglia proporzionalmente, non blocca
        if loss_consecutivi >= self.MAX_LOSS_CONSECUTIVI:
            extra = loss_consecutivi - self.MAX_LOSS_CONSECUTIVI + 1
            loss_f = min(1.50, 1.0 + extra * 0.10)
        else:
            loss_f = 1.0

        soglia_raw = self.SOGLIA_BASE * context_ratio * regime_f * vol_f * history_f * prebreak_f * drift_f * loss_f
        SOGLIA_FLOOR_ASSOLUTO = 48
        soglia_min_ctx = max(SOGLIA_FLOOR_ASSOLUTO, self.SOGLIA_MIN * context_ratio)
        
        # SOGLIA_MAX ADATTIVA PER REGIME - non più fissa
        dynamic_max = self._get_dynamic_soglia_max(regime, volatility)
        soglia = max(soglia_min_ctx, min(dynamic_max, soglia_raw))

        # -- BOOST SOGLIA DA CAPSULE L3 (IntelligenzaAutonoma) -------------
        # Le capsule L3 possono alzare la soglia proporzionalmente alla gravità.
        # Il pavimento SOGLIA_FLOOR_ASSOLUTO=48 rimane inviolabile.
        soglia_boost = kwargs.get('soglia_boost', 0.0)
        if soglia_boost > 0:
            soglia = max(soglia_min_ctx, min(dynamic_max, soglia + soglia_boost))

        # -- DECISIONE -----------------------------------------------------
        # Salva score e soglia per heartbeat/grafico
        self._last_score  = score
        self._last_soglia = soglia
        enter = score >= soglia

        # -- SIZE CONTINUA -------------------------------------------------
        if enter:
            eccedenza = (score - soglia) / max(1.0, score_max - soglia)
            size = self.SIZE_MIN + (self.SIZE_MAX - self.SIZE_MIN) * (eccedenza ** 1.5)
            size = min(self.SIZE_MAX, max(self.SIZE_MIN, size))
        else:
            size = 0.0

        return {
            'enter':     enter,
            'score':     round(score, 2),
            'soglia':    round(soglia, 2),
            'size':      round(size, 3),
            'veto':      None,
            'pb_signals': prebreak_signals,
            'score_max': round(score_max, 1),
            'breakdown': {
                'seed':    round(s_seed, 2),
                'fp':      round(s_fp, 2),
                'mom':     round(s_mom, 2),
                'trend':   round(s_trend, 2),
                'vol':     round(s_vol, 2),
                'regime':  round(s_reg, 2),
                'rsi':     round(s_rsi, 2),
                'macd':    round(s_macd, 2),
                'rsi_val': round(self._last_rsi, 1),
                'score_max': round(score_max, 1),
                'ctx':     round(context_ratio, 2),
                'soglia_f': f"r={regime_f:.2f} v={vol_f:.2f} h={history_f:.2f} d={drift_f:.2f} ctx={context_ratio:.2f} smax={score_max:.0f} RSI={self._last_rsi:.0f} {prebreak_detail} {drift_detail}".strip(),
            }
        }

    def _get_dynamic_soglia_max(self, regime: str, volatility: str) -> float:
        """
        SOGLIA_MAX ADATTIVA - il regime e il contesto decidono il tetto.
        Usa range_position, drift, volatility per calibrare.
        """
        # Calcola range_position dai prezzi recenti
        range_position = 0.5  # default centro
        if len(self._prices_long) >= 200:
            recent = list(self._prices_long)[-200:]
            r_high = max(recent)
            r_low = min(recent)
            r_size = r_high - r_low
            if r_size > 0:
                range_position = (recent[-1] - r_low) / r_size
        
        # Calcola drift
        drift_pct = 0.0
        if len(self._prices_long) >= 100:
            _p = list(self._prices_long)
            _avg_old = sum(_p[:50]) / 50
            _avg_new = sum(_p[-50:]) / 50
            drift_pct = (_avg_new - _avg_old) / _avg_old * 100
        
        if regime == "RANGING":
            base = 80
            # Centro del range → più selettivo
            if 0.40 <= range_position <= 0.60:
                base = 83
                if volatility == "ALTA":
                    base += 2  # 85 - molto selettivo al centro con alta vol
            # Bordi del range → più permissivo
            elif range_position <= 0.25 or range_position >= 0.75:
                base = 76
                if abs(drift_pct) >= 0.10:
                    base -= 2  # 74 - drift vero al bordo, lascia entrare
        
        elif regime == "TRENDING_BULL":
            base = 70
            if drift_pct > 0.10:
                base = 66  # trend confermato, più permissivo
            if volatility == "BASSA":
                base -= 2  # trend pulito, ancora più permissivo
        
        elif regime == "TRENDING_BEAR":
            base = 75
            if drift_pct < -0.10:
                base = 72  # trend bear confermato
        
        elif regime == "EXPLOSIVE":
            base = 75   # EXPLOSIVE rischiosa — serve segnale forte
            if volatility == "ALTA":
                base = 72  # ancora alta — esplosione vera ma volatilità aumenta rischio
        
        else:
            base = 80
        
        return float(base)

    def record_result(self, is_win: bool, exit_reason: str = "", pb_signals: int = 0, pnl: float = 0.0):
        """Chiamato alla chiusura di ogni shadow trade."""
        self._recent_results.append(is_win)

        # -- META-REGOLA: PREBREAKOUT AUTO-TUNING -------------------------
        # Traccia i risultati dei trade che sono entrati con pb=3/3
        if pb_signals >= 3:
            self._pb3_results.append({
                'is_win': is_win,
                'exit': exit_reason,
                'pnl': pnl,
            })

            # Dopo 5+ trade pb3, valuta se le soglie vanno strette
            if len(self._pb3_results) >= 5:
                pb3_list = list(self._pb3_results)
                pb3_wins = sum(1 for r in pb3_list if r['is_win'])
                pb3_wr = pb3_wins / len(pb3_list)
                pb3_smorz = sum(1 for r in pb3_list if r['exit'] == 'SMORZ' and not r['is_win'])

                # Se WR pb3 < 50% → stringi compressione (da 0.0003 a 0.0002)
                if pb3_wr < 0.50 and self._pb3_compression_threshold > 0.00015:
                    self._pb3_compression_threshold -= 0.00005
                    log.info(f"[META] 🧠 PreBreakout auto-tune: WR pb3={pb3_wr:.0%} < 50% → "
                             f"compression threshold stretto a {self._pb3_compression_threshold:.5f}")

                # Se > 40% dei LOSS pb3 escono per SMORZ → alza vol_acc threshold
                if len(pb3_list) >= 5:
                    smorz_ratio = pb3_smorz / max(1, len(pb3_list) - pb3_wins)
                    if smorz_ratio > 0.40 and self._pb3_vol_acc_threshold < 3.0:
                        self._pb3_vol_acc_threshold += 0.2
                        log.info(f"[META] 🧠 PreBreakout auto-tune: SMORZ ratio={smorz_ratio:.0%} > 40% → "
                                 f"vol_acc threshold alzato a {self._pb3_vol_acc_threshold:.1f}")

                # Se WR pb3 > 70% → allenta (le soglie funzionano)
                if pb3_wr > 0.70 and self._pb3_compression_threshold < 0.0003:
                    self._pb3_compression_threshold += 0.00002
                    log.info(f"[META] 🧠 PreBreakout auto-tune: WR pb3={pb3_wr:.0%} > 70% → "
                             f"compression threshold allentato a {self._pb3_compression_threshold:.5f}")

    def _history_factor(self) -> float:
        """Soglia sale dopo loss streak ma decade nel tempo (5 min).
        Il pugile alza le braccia ma le riabbassa se non arrivano pugni."""
        if len(self._recent_results) < 5:
            return 1.0
        recent_wr = sum(1 for r in self._recent_results if r) / len(self._recent_results)
        if recent_wr < 0.40:
            if not hasattr(self, '_history_factor_since'):
                self._history_factor_since = time.time()
            elapsed = time.time() - self._history_factor_since
            decay = max(0.0, 1.0 - elapsed / 300.0)
            return 1.0 + (0.20 * decay)
        if hasattr(self, '_history_factor_since'):
            del self._history_factor_since
        return 1.0

    def _pre_breakout_factor(self) -> tuple:
        """
        ★ PRE-BREAKOUT DETECTOR - il cecchino sente i passi.

        Tre segnali indipendenti:
          1. COMPRESSIONE: range stretto (< 0.02%) con volatilita storica alta
          2. VOLUME CRESCENTE: vol_accel > 1.3 a prezzo fermo
          3. SEED CRESCENTI: derivata positiva per 5+ tick consecutivi

        Ogni segnale vale 0.0-1.0. Il fattore finale è:
          3 segnali attivi → 0.70 (soglia scende del 30%) ← UNICO CHE FUNZIONA
          2 segnali attivi → 0.96 (quasi invariata - dati dicono che perde)
          1 segnale attivo → 1.00 (nessun effetto)
          0 segnali        → 1.00 (nessun effetto)

        Ritorna (factor: float, dettaglio: str)
        """
        if len(self._prices_short) < 30 or len(self._seed_history) < 5:
            return 1.0, "", 0

        signals = 0
        details = []

        # -- 1. COMPRESSIONE -----------------------------------------------
        prices = list(self._prices_short)
        recent_50 = prices[-50:] if len(prices) >= 50 else prices
        p_max = max(recent_50)
        p_min = min(recent_50)
        p_mid = (p_max + p_min) / 2
        compression = (p_max - p_min) / p_mid if p_mid > 0 else 1.0

        if compression < self._pb3_compression_threshold:   # ADATTIVO - si stringe se pb3 WR < 50%
            signals += 1
            details.append(f"COMPRESS={compression:.5f}")

        # -- 2. VOLUME CRESCENTE (a prezzo fermo) -------------------------
        if len(self._volumes_short) >= 20:
            vols = list(self._volumes_short)
            vol_recent = sum(vols[-10:]) / 10
            vol_prev   = sum(vols[-20:-10]) / 10
            if vol_prev > 0:
                vol_ratio = vol_recent / vol_prev
                if vol_ratio > self._pb3_vol_acc_threshold and compression < 0.001:   # ADATTIVO
                    # Volume sale MA prezzo fermo → accumulazione
                    signals += 1
                    details.append(f"VOL_ACC={vol_ratio:.2f}")

        # -- 3. SEED DIREZIONALI (derivata positiva per LONG, negativa per SHORT) --
        seeds = list(self._seed_history)
        if len(seeds) >= 5:
            last_5 = seeds[-5:]
            if self._direction == "SHORT":
                # SHORT: seed DECRESCENTI = impulso ribassista che nasce
                all_directed = all(last_5[i] < last_5[i-1] for i in range(1, len(last_5)))
                directed_count = sum(1 for i in range(1, len(last_5)) if last_5[i] < last_5[i-1])
                avg_deriv = (last_5[0] - last_5[-1]) / 4  # positivo se scende
            else:
                # LONG: seed CRESCENTI = impulso rialzista che nasce
                all_directed = all(last_5[i] > last_5[i-1] for i in range(1, len(last_5)))
                directed_count = sum(1 for i in range(1, len(last_5)) if last_5[i] > last_5[i-1])
                avg_deriv = (last_5[-1] - last_5[0]) / 4

            if all_directed and avg_deriv > 0.02:
                signals += 1
                details.append(f"SEED_DIR={avg_deriv:+.3f}")
            elif directed_count >= 4 and avg_deriv > 0.05:
                signals += 1
                details.append(f"SEED_DIR4={avg_deriv:+.3f}")

        # -- CALCOLA FATTORE -----------------------------------------------
        # CALIBRATO SU DATI REALI (6 trade shadow 16/03/2026):
        #   3/3 segnali: 2 WIN +$27.29, 1 LOSS -$1.28 → WR 66%, R/R 21:1
        #   2/3 segnali: 0 WIN, 3 LOSS -$0.78 → WR 0% → QUASI DISABILITATO
        #   Solo il 3/3 pieno abbassa significativamente la soglia.
        if signals >= 3:
            factor = 0.92    # segnala ma NON crea buchi - i LOSS SMORZ a soglia bassa sono la prova
        elif signals >= 2:
            factor = 0.96    # quasi nessun effetto - 2/3 perde troppo
        elif signals >= 1:
            factor = 1.00    # un segnale = nessun effetto
        else:
            factor = 1.00    # niente - soglia invariata

        detail_str = f"pb={factor:.2f}({signals}/3 {'+'.join(details)})" if signals > 0 else ""
        return factor, detail_str, signals

    def _drift_factor(self) -> tuple:
        """
        ★ DRIFT DETECTOR - in che direzione soffia il vento?
        
        RANGING: il drift oscilla costantemente ±0.05%. Non è un segnale,
        è rumore. Il fattore è DIMEZZATO per non bloccare trade buoni.
        
        TRENDING: il drift è un segnale reale. Fattore pieno.
        """
        if len(self._prices_long) < 100:
            return 1.0, ""

        prices = list(self._prices_long)
        avg_old    = sum(prices[:50]) / 50
        avg_recent = sum(prices[-50:]) / 50

        if avg_old == 0:
            return 1.0, ""

        drift_pct = (avg_recent - avg_old) / avg_old * 100

        if drift_pct < -0.10:
            factor = 1.30
            detail = f"DRIFT={drift_pct:+.3f}%↓↓"
        elif drift_pct < -0.03:
            factor = 1.15
            detail = f"DRIFT={drift_pct:+.3f}%↓"
        elif drift_pct > 0.05:
            factor = 0.95
            detail = f"DRIFT={drift_pct:+.3f}%↑"
        else:
            return 1.0, ""

        # In RANGING il drift è rumore - dimezza l'effetto
        # factor 1.15 → 1.075, factor 1.30 → 1.15
        # Così un drift -0.04% non alza la soglia da 49 a 56 ma solo a 53
        if hasattr(self, '_last_regime_for_drift'):
            regime = self._last_regime_for_drift
        else:
            regime = "RANGING"
        if regime == "RANGING":
            factor = 1.0 + (factor - 1.0) * 0.5
            detail += " (R½)"

        return factor, detail

    # -- RSI + MACD: I CONSIGLIERI ----------------------------------------

    def _update_rsi(self):
        """Calcola RSI a 14 periodi sui prezzi recenti."""
        prices = list(self._prices_ta)
        if len(prices) < self._rsi_period + 1:
            return
        changes = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
        recent = changes[-(self._rsi_period):]
        gains = [c for c in recent if c > 0]
        losses_raw = [-c for c in recent if c < 0]
        avg_gain = sum(gains) / self._rsi_period if gains else 0
        avg_loss = sum(losses_raw) / self._rsi_period if losses_raw else 0.001
        rs = avg_gain / max(avg_loss, 0.001)
        self._last_rsi = 100 - (100 / (1 + rs))

    def _update_macd(self):
        """Calcola MACD (12/26/9) sui prezzi recenti."""
        prices = list(self._prices_ta)
        if len(prices) < self._macd_slow + self._macd_signal:
            return

        def ema(data, period):
            """EMA semplificata."""
            if len(data) < period:
                return sum(data) / len(data) if data else 0
            mult = 2 / (period + 1)
            result = sum(data[:period]) / period
            for val in data[period:]:
                result = (val - result) * mult + result
            return result

        ema_fast = ema(prices, self._macd_fast)
        ema_slow = ema(prices, self._macd_slow)
        self._last_macd = ema_fast - ema_slow
        self._last_macd_signal = self._last_macd * 0.8
        self._last_macd_hist = self._last_macd - self._last_macd_signal

    def _rsi_score(self) -> float:
        """
        ★ RSI CONSIGLIERE - ipervenduto o ipercomprato?
        LONG:  RSI < 30 = buono (ipervenduto, rimbalzo) | RSI > 70 = cattivo (ipercomprato)
        SHORT: RSI > 70 = buono (ipercomprato, crollo)  | RSI < 30 = cattivo (ipervenduto)
        """
        rsi = self._last_rsi
        if self._direction == "SHORT":
            # Invertito: ipercomprato = buono per SHORT
            if rsi > 75:   return 1.0
            elif rsi > 65: return 0.80
            elif rsi > 55: return 0.60
            elif rsi > 45: return 0.40
            elif rsi > 35: return 0.30
            elif rsi > 25: return 0.15
            else:          return 0.0
        else:
            # LONG: ipervenduto = buono
            if rsi < 25:   return 1.0
            elif rsi < 35: return 0.80
            elif rsi < 45: return 0.60
            elif rsi < 55: return 0.40
            elif rsi < 65: return 0.30
            elif rsi < 75: return 0.15
            else:          return 0.0

    def _macd_score(self) -> float:
        """
        ★ MACD CONSIGLIERE - il trend sta nascendo o morendo?
        LONG:  MACD positivo crescente = buono | negativo decrescente = cattivo
        SHORT: MACD negativo decrescente = buono | positivo crescente = cattivo
        """
        macd = self._last_macd
        hist = self._last_macd_hist
        if self._direction == "SHORT":
            # Invertito: bearish = buono per SHORT
            if macd < 0 and hist < 0:    return 1.0    # bearish forte
            elif hist < 0:                return 0.70   # sotto signal
            elif abs(hist) < abs(macd) * 0.1 if macd != 0 else True: return 0.40
            elif hist > 0 and macd < 0:   return 0.25
            elif hist > 0 and macd > 0:   return 0.0    # bullish forte = cattivo per SHORT
            return 0.35
        else:
            # LONG: bullish = buono
            if macd > 0 and hist > 0:    return 1.0
            elif hist > 0:                return 0.70
            elif abs(hist) < abs(macd) * 0.1 if macd != 0 else True: return 0.40
            elif hist < 0 and macd > 0:   return 0.25
            elif hist < 0 and macd < 0:   return 0.0
            return 0.35

    def _veto(self, reason: str) -> dict:
        return {'enter': False, 'score': 0.0, 'soglia': 0.0,
                'size': 0.0, 'veto': reason, 'pb_signals': 0, 'breakdown': {}}

    def get_stats(self) -> dict:
        total = len(self._recent_results)
        if total == 0:
            return {'trades': 0, 'wr': 0.0, 'rsi': round(self._last_rsi, 1), 
                    'macd': round(self._last_macd, 4), 'macd_hist': round(self._last_macd_hist, 4),
                    'drift_veto_threshold': self.DRIFT_VETO_THRESHOLD,
                    'soglia_max': self.SOGLIA_MAX, 'direction': self._direction}
        wins = sum(1 for r in self._recent_results if r)
        return {'trades': total, 'wr': round(wins / total, 3),
                'wins': wins, 'losses': total - wins,
                'rsi': round(self._last_rsi, 1),
                'macd': round(self._last_macd, 4),
                'macd_hist': round(self._last_macd_hist, 4),
                'drift_veto_threshold': self.DRIFT_VETO_THRESHOLD,
                'soglia_max': self.SOGLIA_MAX, 'direction': self._direction}


# ===========================================================================
# ★★★ BOT PRINCIPALE - OVERTOP BASSANO V14 PRODUCTION ★★★
# ===========================================================================

class VeritatisTracker:
    """
    Tracker della Verità — confronta in tempo reale chi aveva ragione.
    
    Ogni volta che l'Oracolo dice FUOCO registra il momento.
    Dopo 30/60 secondi misura dove è andato il prezzo.
    Confronta con cosa diceva il SuperCervello nello stesso istante.
    
    Non aspetta trade confermati — usa delta_30/60s come verità.
    Dopo 50 segnali sa chi aveva ragione e di quanto.
    """
    
    def __init__(self, sc_ref=None):
        self._open   = []   # segnali aperti in attesa di conferma
        self._closed = []   # segnali chiusi con verità nota
        self._sc_ref = sc_ref  # SuperCervello per calibrazione automatica
        self._stats  = {    # statistiche per combinazione
            # chiave: f"{oi_stato}|{sc_decisione}"
            # es: "FUOCO|BLOCCA" o "FUOCO|ENTRA" o "CARICA|BLOCCA"
        }
    
    def registra(self, price: float, oi_stato: str, oi_carica: float,
                 sc_decisione: str, sc_confidenza: float,
                 regime: str, ts: float):
        """Registra un segnale al momento della decisione."""
        self._open.append({
            'price':        price,
            'oi_stato':     oi_stato,
            'oi_carica':    round(oi_carica, 3),
            'sc_decisione': sc_decisione,
            'sc_conf':      round(sc_confidenza, 3),
            'regime':       regime,
            'ts':           ts,
            'chiave':       f"{oi_stato}|{sc_decisione}",
        })
    
    def aggiorna(self, price_now: float, ts_now: float):
        """
        Ogni tick aggiorna i segnali aperti.
        Chiude quelli con 30/60/120 secondi trascorsi.
        """
        ancora_aperti = []
        for sig in self._open:
            elapsed = ts_now - sig['ts']
            delta   = price_now - sig['price']
            # Hit vero: il delta deve coprire le fee reali
            # $1000 margine × 5x leva = $5000 esposti
            # Fee: $5000 × 0.02% × 2 lati = $2.00
            pnl_sim = delta / sig['price'] * 5000 * 0.7 - 2.0
            hit     = delta > 0  # direzione corretta
            
            if elapsed >= 60:
                # Chiudi con verità a 60 secondi
                sig['delta_60'] = round(delta, 2)
                sig['hit_60']   = hit
                sig['pnl_60']   = round(pnl_sim, 2)
                sig['elapsed']  = round(elapsed, 1)
                self._closed.append(sig)
                if len(self._closed) > 500:
                    self._closed.pop(0)
                # Aggiorna statistiche
                self._aggiorna_stats(sig)
            else:
                ancora_aperti.append(sig)
        
        self._open = ancora_aperti
    
    def _aggiorna_stats(self, sig: dict):
        """Aggiorna statistiche per chiave oi_stato|sc_decisione."""
        k = sig['chiave']
        if k not in self._stats:
            self._stats[k] = {
                'n': 0, 'hits': 0, 'pnl': 0.0,
                'deltas': [], 'oi_carica_avg': 0.0,
            }
        s = self._stats[k]
        s['n']    += 1
        s['hits'] += sig['hit_60']
        s['pnl']  += sig['pnl_60']
        s['deltas'].append(sig['delta_60'])
        if len(s['deltas']) > 100: s['deltas'].pop(0)
        # Media carica
        s['oi_carica_avg'] = round(
            (s['oi_carica_avg'] * (s['n']-1) + sig['oi_carica']) / s['n'], 3)
        # Calibra SC automaticamente dal verdetto
        if self._sc_ref and s['n'] >= 10:
            self._calibra_sc(sig, s)

    def _calibra_sc(self, sig: dict, stats: dict):
        """
        Ogni volta che il Veritas chiude un segnale con n>=10,
        aggiusta i pesi del SuperCervello in base al verdetto reale.
        
        Logica:
        - Oracolo FUOCO con SC BLOCCA e hit_rate >= 0.60 → SC era sbagliato
          → aumenta peso campo_carica (organo dell'Oracolo)
          → riduci peso signal_tracker (troppo conservativo)
        - Oracolo FUOCO con SC BLOCCA e hit_rate <= 0.40 → SC aveva ragione
          → aumenta peso signal_tracker
          → riduci peso campo_carica
        - FUOCO_SHORT con BLOCCA e hit_rate >= 0.60 → SHORT bloccato era giusto
          → aumenta campo_carica per SHORT
        """
        if not self._sc_ref:
            return
            
        hit_rate = stats['hits'] / stats['n']
        chiave   = sig['chiave']
        pesi     = self._sc_ref._pesi
        STEP     = 0.008  # step piccolo — cambiamento graduale
        
        try:
            if 'FUOCO' in chiave and 'BLOCCA' in chiave:
                if hit_rate >= 0.60:
                    # Oracolo aveva ragione — SC bloccava
                    # campo_carica sale più velocemente fino a max 0.60
                    pesi['campo_carica']   = min(0.60, pesi['campo_carica']   + STEP * 2)
                    pesi['signal_tracker'] = max(0.05, pesi['signal_tracker'] - STEP)
                    pesi['oracolo_fp']     = max(0.05, pesi['oracolo_fp']     - STEP/2)
                    pesi['matrimonio']     = max(0.05, pesi['matrimonio']     - STEP/2)
                elif hit_rate <= 0.40:
                    # SC aveva ragione a bloccare
                    pesi['signal_tracker'] = min(0.45, pesi['signal_tracker'] + STEP)
                    pesi['campo_carica']   = max(0.05, pesi['campo_carica']   - STEP)

            elif 'FUOCO' in chiave and 'ENTRA' in chiave:
                if hit_rate >= 0.60:
                    # Oracolo + SC concordavano e avevano ragione
                    pesi['campo_carica'] = min(0.60, pesi['campo_carica'] + STEP)
                    pesi['oracolo_fp']   = min(0.40, pesi['oracolo_fp']   + STEP/2)
                elif hit_rate <= 0.40:
                    pesi['campo_carica'] = max(0.05, pesi['campo_carica'] - STEP)

            # Pavimento/soffitto pesi — campo_carica non scende mai sotto 30%
            # signal_tracker non sale mai sopra 25% — non deve dominare
            pesi['campo_carica']   = max(0.30, pesi['campo_carica'])
            pesi['signal_tracker'] = min(0.25, pesi['signal_tracker'])
            pesi['oracolo_fp']     = max(0.15, pesi['oracolo_fp'])
            # Rinormalizza sempre a somma 1.0
            tot = sum(pesi.values())
            for k in pesi:
                pesi[k] = round(pesi[k] / tot, 4)
                
        except Exception:
            pass  # mai crashare per calibrazione
    
    def verdetto(self) -> dict:
        """
        Calcola il verdetto finale:
        - Chi aveva ragione: Oracolo o SuperCervello?
        - Quanto valore ha bloccato il SC?
        - Quanto ha protetto?
        """
        risultati = {}
        for k, s in self._stats.items():
            if s['n'] < 3:
                continue
            hit_rate = s['hits'] / s['n']
            pnl_avg  = s['pnl'] / s['n']
            risultati[k] = {
                'n':         s['n'],
                'hit_rate':  round(hit_rate, 3),
                'pnl_avg':   round(pnl_avg, 2),
                'pnl_tot':   round(s['pnl'], 2),
                'carica_avg':s['oi_carica_avg'],
                'verdetto':  'GIUSTO' if pnl_avg > 0 else 'SBAGLIATO',
            }
        
        # Calcola chi aveva ragione nei conflitti
        fuoco_entra = risultati.get('FUOCO|ENTRA', {})
        fuoco_blocca = risultati.get('FUOCO|BLOCCA', {})
        
        conflitto = {}
        if fuoco_entra and fuoco_blocca:
            pnl_entra  = fuoco_entra.get('pnl_avg', 0)
            pnl_blocca = fuoco_blocca.get('pnl_avg', 0)
            # Se bloccare aveva PnL positivo → SC aveva ragione
            # Se bloccare aveva PnL negativo → Oracolo aveva ragione
            if pnl_blocca > 0:
                conflitto['chi_aveva_ragione'] = 'SC'
                conflitto['pnl_perso_bloccando'] = fuoco_blocca.get('pnl_tot', 0)
            else:
                conflitto['chi_aveva_ragione'] = 'ORACOLO'
                conflitto['pnl_salvato_bloccando'] = abs(fuoco_blocca.get('pnl_tot', 0))
        
        return {'stats': risultati, 'conflitto': conflitto, 'n_closed': len(self._closed)}
    
    def dump_dashboard(self) -> dict:
        """Dati per la dashboard."""
        v = self.verdetto()
        rows = []
        for k, s in v['stats'].items():
            oi, sc = k.split('|')
            rows.append({
                'chiave':   k,
                'oi':       oi,
                'sc':       sc,
                'n':        s['n'],
                'hit_rate': s['hit_rate'],
                'pnl_avg':  s['pnl_avg'],
                'pnl_tot':  s['pnl_tot'],
                'verdetto': s['verdetto'],
            })
        rows.sort(key=lambda x: -x['n'])
        return {
            'rows':      rows,
            'conflitto': v['conflitto'],
            'n_closed':  v['n_closed'],
            'n_open':    len(self._open),
        }

    def save(self, db_path: str):
        """Persiste _stats e _closed su SQLite — sopravvive al restart."""
        try:
            import sqlite3, json
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS veritas_stats
                         (chiave TEXT PRIMARY KEY, data TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS veritas_closed
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT)""")
            # Salva stats
            for k, s in self._stats.items():
                c.execute("INSERT OR REPLACE INTO veritas_stats VALUES (?,?)",
                          (k, json.dumps(s)))
            # Salva ultimi 200 closed (non duplicare)
            c.execute("DELETE FROM veritas_closed")
            for sig in self._closed[-200:]:
                c.execute("INSERT INTO veritas_closed (data) VALUES (?)",
                          (json.dumps(sig),))
            conn.commit(); conn.close()
        except Exception as e:
            log.error(f"[VERITAS_SAVE] {e}")

    def load(self, db_path: str):
        """Carica _stats e _closed da SQLite al boot."""
        try:
            import sqlite3, json, os
            if not os.path.exists(db_path): return
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            # Stats
            try:
                for row in c.execute("SELECT chiave, data FROM veritas_stats"):
                    self._stats[row[0]] = json.loads(row[1])
            except: pass
            # Closed
            try:
                for row in c.execute("SELECT data FROM veritas_closed ORDER BY id"):
                    self._closed.append(json.loads(row[0]))
            except: pass
            conn.close()
            if self._stats:
                log.info(f"[VERITAS_LOAD] Caricati {len(self._stats)} stats, {len(self._closed)} closed")
        except Exception as e:
            log.error(f"[VERITAS_LOAD] {e}")


class SuperCervello:
    """
    Supercervello — legge tutti gli organi simultaneamente ogni tick.
    Produce una decisione unica: ENTRA / ATTENDI / BLOCCA
    con size_mult e soglia_adj calcolati dai voti pesati.
    I pesi si adattano autonomamente dopo ogni trade.
    """
    PESI_DEFAULT = {
        'oracolo_fp':    0.25,
        'signal_tracker':0.20,
        'campo_carica':  0.30,
        'matrimonio':    0.13,
        'phantom_ratio': 0.12,
    }

    def __init__(self):
        self._pesi = dict(self.PESI_DEFAULT)
        self._storia = []
        self._n = 0

    def decide(self, fp_wr, fp_samples, st_hit_rate, st_n, st_pnl,
               oi_carica, oi_stato, score, soglia,
               matrimonio_wr, matrimonio_trust,
               ph_protezione, ph_zavorra,
               regime, midzone, loss_streak) -> dict:

        self._n += 1

        # Blocchi assoluti
        if midzone:
            return self._out("BLOCCA", 0.5, 0, "midzone", 0.95)
        if loss_streak >= 4:
            return self._out("BLOCCA", 0.5, 0, f"streak_{loss_streak}", 0.90)

        # VERITAS: Oracolo FUOCO con carica alta — SC non blocca mai
        # 373 segnali: SC blocca $112 di guadagni reali quando Oracolo ha ragione
        # Carica 0.90 = fisica confermata — entra sempre
        # ECCEZIONE: EXPLOSIVE è troppo volatile — FUOCO non affidabile
        if oi_stato == "FUOCO" and oi_carica >= 0.75 and regime != "EXPLOSIVE":
            return self._out("ENTRA", 1.3, -5, f"VERITAS_FUOCO_c{oi_carica:.2f}", oi_carica)

        # VETO ASSOLUTO FINGERPRINT TOSSICO
        # Se il fingerprint ha 20+ campioni con WR < 45% — blocca sempre
        # La memoria storica dell'Oracolo è il giudice più affidabile
        if fp_samples >= 20 and fp_wr < 0.45:
            return self._out("BLOCCA", 0.5, 0, f"FP_TOSSICO_wr={fp_wr:.0%}_n={fp_samples}", 0.95)

        # BOOST PREDIZIONE: se score >85% e calibrazione >85% e Oracolo FUOCO → entra
        # La predizione è dimostrata dal Veritas — l'Oracolo ha ragione
        _ps = getattr(self, '_pred_score_ref', 0)
        _pc = getattr(self, '_pred_calib_ref', 0)
        if _ps >= 85 and _pc >= 85 and oi_stato == "FUOCO" and not midzone:
            return self._out("ENTRA", 1.2, -5,
                f"pred_boost score={_ps:.0f}% calib={_pc:.0f}%", 0.85)

        # Voti organi
        v = {}
        # Fingerprint
        v['oracolo_fp'] = (1.0 if fp_wr>=0.70 and fp_samples>=10 else
                           0.6 if fp_wr>=0.55 and fp_samples>=10 else
                           0.0 if fp_wr<=0.35 and fp_samples>=10 else 0.5)
        # Signal Tracker
        v['signal_tracker'] = (1.0 if st_hit_rate>=0.65 and st_pnl>0 and st_n>=10 else
                                0.6 if st_hit_rate>=0.55 and st_n>=10 else
                                0.0 if (st_hit_rate<=0.40 or st_pnl<-1) and st_n>=10 else 0.5)
        # Carica — peso dinamico basato sulla carica reale dell'Oracolo
        # Più alta la carica → più peso all'organo che la esprime
        if oi_stato == "FUOCO":
            if oi_carica >= 0.80:   v['campo_carica'] = 1.0   # massima fiducia
            elif oi_carica >= 0.65: v['campo_carica'] = 0.85
            else:                   v['campo_carica'] = 0.70
        elif oi_stato == "CARICA":
            if oi_carica >= 0.80:   v['campo_carica'] = 0.75
            elif oi_carica >= 0.65: v['campo_carica'] = 0.60
            else:                   v['campo_carica'] = 0.45
        else:
            v['campo_carica'] = 0.1
        # Matrimonio
        v['matrimonio'] = (1.0 if matrimonio_trust>=0.7 and matrimonio_wr>=0.65 else
                           0.6 if matrimonio_wr>=0.55 else
                           0.0 if matrimonio_wr<=0.40 else 0.5)
        # Phantom
        if ph_protezione + ph_zavorra > 0:
            r = ph_protezione / (ph_protezione + ph_zavorra + 0.01)
            v['phantom_ratio'] = 1.0 if r>=0.80 else 0.7 if r>=0.60 else 0.2 if r<=0.40 else 0.5
        else:
            v['phantom_ratio'] = 0.5

        # Score pesato
        st = sum(v[k] * self._pesi[k] for k in v)

        if st >= 0.68:
            azione = "ENTRA"
            sm = round(min(2.0, max(0.7, 0.7 + (st-0.68)/0.32*1.3)), 2)
            sa = -3 if st >= 0.80 else 0
        elif st >= 0.50:
            azione = "ATTENDI"; sm = 1.0; sa = 0
        else:
            azione = "BLOCCA";  sm = 0.5; sa = +3

        pro    = sum(1 for x in v.values() if x >= 0.6)
        contro = sum(1 for x in v.values() if x <= 0.3)
        motivo = f"sc={st:.2f} pro={pro}/5 contro={contro}/5"

        return self._out(azione, sm, sa, motivo, st, v)

    def registra_esito(self, dec: dict, win: bool):
        """Dopo ogni trade adatta i pesi — gli organi precisi pesano di più."""
        self._storia.append({'voti': dec.get('voti',{}),'win': win})
        if len(self._storia) < 10: return
        ultimi = self._storia[-30:]
        for organo in self._pesi:
            vw = [t['voti'].get(organo,0.5) for t in ultimi if t['win']]
            vl = [t['voti'].get(organo,0.5) for t in ultimi if not t['win']]
            if not vw or not vl: continue
            disc = sum(vw)/len(vw) - sum(vl)/len(vl)
            if disc >= 0.15:
                self._pesi[organo] = min(0.45, self._pesi[organo]*1.05)
            elif disc <= -0.10:
                self._pesi[organo] = max(0.05, self._pesi[organo]*0.95)
        tot = sum(self._pesi.values())
        for k in self._pesi: self._pesi[k] = round(self._pesi[k]/tot, 4)

    def _out(self, azione, size_mult, soglia_adj, motivo, confidenza, voti={}):
        return {'azione':azione,'size_mult':size_mult,'soglia_adj':soglia_adj,
                'motivo':motivo,'confidenza':round(confidenza,3),
                'voti':voti,'pesi':dict(self._pesi)}


class OvertopBassanoV15Production:
    """
    Bot BTC/USDC su Binance WebSocket.
    Modalita: PAPER_TRADE (simula) o LIVE (ordini reali).

    Architettura decisionale entry:
      SeedScorer → OracoloDinamico → MemoriaMatrimoni → 5 Capsule → CapsuleRuntime

    Architettura exit:
      4 Divorce Triggers (ogni tick) → SMORZ (impulso finito) → Timeout adattivo

    Auto-apprendimento:
      OracoloDinamico aggiorna WR fingerprint ad ogni trade chiuso.
      RealtimeLearningEngine genera capsule di blocco se WR < 40% su 3+ campioni.
      MemoriaMatrimoni scala trust e irroga SEPARAZIONE/DIVORZIO.
    """

    def __init__(self, heartbeat_data=None, db_execute=None, heartbeat_lock=None):
        self.symbol         = SYMBOL
        self.ws_url         = BINANCE_WS_URL
        self.paper_trade    = PAPER_TRADE

        self.heartbeat_data = heartbeat_data if heartbeat_data is not None else {}
        self.heartbeat_lock = heartbeat_lock
        self.db_execute     = db_execute

        # -- Persistenza --------------------------------------------------
        self._persist        = PersistenzaStato(db_path=DB_PATH)
        self.capital, self.total_trades = self._persist.load()
        self.TRADE_SIZE_USD = 1000.0  # SIZE FISSA $1000 margine per trade
        self.LEVERAGE = 5             # LEVA 5x - $1000 margine = $5000 esposizione
        self.FEE_PCT = 0.0002        # 0.02% maker futures (vs 0.075% spot)
        self.wins    = 0
        self.losses  = 0

        # -- Componenti core ----------------------------------------------
        self.analyzer        = ContestoAnalyzer(window=50)
        self.seed_scorer     = SeedScorer(window=50)
        self.oracolo         = OracoloDinamico()
        self.memoria         = MemoriaMatrimoni()

        # -- CAPSULE MANAGER UNIFICATO ------------------------------------
        if _CM_AVAILABLE:
            self.capsule_manager = CapsuleManager(db_path=DB_PATH, asset=SYMBOL)
            # Alias per compatibilità con codice esistente
            self.capsule_runtime = self.capsule_manager
            self.config_reloader = self.capsule_manager
            self.realtime_engine = self.capsule_manager
            log.info(f"[CM] ✅ CapsuleManager attivo — asset={SYMBOL}")
        else:
            # Fallback ai sistemi originali
            self.capsule_manager = None
            self.capsule_runtime = CapsuleRuntime(capsule_file="capsule_attive.json")
            self.config_reloader = ConfigHotReloader(capsule_path="capsule_attive.json")
            self.realtime_engine = IntelligenzaAutonoma(capsule_file="capsule_attive.json", db_path=DB_PATH)
            log.warning("[CM] ⚠️ Fallback ai sistemi originali")
        # -----------------------------------------------------------------

        self.log_analyzer    = LogAnalyzer()
        self.ai_explainer    = AIExplainer(db_path=NARRATIVES_DB)
        self.calibratore     = AutoCalibratore()
        self.regime_detector = RegimeDetector()
        self.decelero        = MomentumDecelerometer()
        self.position_sizer  = PositionSizer()
        self.telemetry       = StabilityTelemetry()
        self.signal_tracker  = PreTradeSignalTracker()

        # -- Ripristina intelligenza accumulata ----------------------------
        self._persist.load_brain(self.oracolo, self.memoria, self.calibratore)
        self._persist.load_signal_tracker(self.signal_tracker)
        self._persist.load_runtime_state(self)
        self._regime_current = 'RANGING'
        self._regime_conf    = 0.0
        self._last_regime_check = time.time()

        # -- 5 Capsule -----------------------------------------------------
        self.capsule1 = Capsule1Coerenza()
        self.capsule2 = Capsule2Trappola()
        self.capsule3 = Capsule3Protezione()
        self.capsule4 = Capsule4Opportunita()
        self.capsule5 = Capsule5Tattica()

        # -- Stato trade ---------------------------------------------------
        self.trade_open         = None   # None = nessun trade aperto
        self.entry_time         = None
        self.entry_momentum     = None   # per divorce trigger 2
        self.entry_volatility   = None   # per divorce trigger 1
        self.entry_fingerprint  = None   # per divorce trigger 4
        self.entry_trend        = None
        self.max_price          = None
        self.current_matrimonio = None

        # -- Timing --------------------------------------------------------
        self.last_heartbeat    = time.time()
        self.last_config_check = time.time()
        self.last_persist      = time.time()
        self.ws                = None

        # -- Stato exit (per capsule reattive) -----------------------------
        self._last_exit_type     = None
        self._last_exit_duration = 0.0
        self._last_entry_seed    = 0.0   # per AutoCalibratore
        self._last_entry_fp_wr   = 0.72  # per AutoCalibratore
        self._trades_since_calib = 0     # contatore per calibrazione

        # -- Log live decisioni (ultimi 20 eventi) -------------------------
        self._live_log = deque(maxlen=20)

        # -- MOTORE 2: CAMPO GRAVITAZIONALE (shadow trading) --------------
        self.campo = CampoGravitazionale()
        self.campo._bot_ref = self  # riferimento al bot per CapsuleManager
        self._shadow = None          # shadow trade aperto (dict o None)
        self._shadow_entry_time = None
        self._shadow_entry_momentum = None
        self._shadow_entry_volatility = None
        self._shadow_entry_trend = None
        self._shadow_entry_fingerprint = None
        self._shadow_max_price = None
        self._shadow_min_price = None
        self._shadow_matrimonio = None
        # -- STATE ENGINE - AGGRESSIVO / NEUTRO / DIFENSIVO ----------------
        # Il tempismo. Non solo COSA fare, ma QUANDO NON FARLO.
        self._state = "NEUTRO"                   # AGGRESSIVO | NEUTRO | DIFENSIVO
        self._state_since = time.time()           # quando è entrato nello stato corrente
        self._state_min_duration = 120            # minimo 2 minuti in ogni stato
        self._m2_recent_trades = deque(maxlen=10) # ultimi 10 trade M2: {'ts', 'pnl', 'is_win', 'duration'}
        self._m2_last_loss_time = 0               # timestamp dell'ultimo loss
        self._m2_loss_streak = 0                  # loss consecutivi correnti
        self._m2_cooldown_until = 0               # non entrare fino a questo timestamp
        # -- AUTO-TUNING SOGLIA - impara dai phantom ----------------------
        # Il sistema legge i propri phantom e aggiusta SOGLIA_MIN automaticamente.
        # Se i phantom bloccati hanno WR > 60% su 10+ campioni → soglia troppo alta.
        # Se WR < 40% → soglia troppo bassa. Rate limit: 1 aggiustamento ogni 15 min.
        self._last_soglia_autotune = 0            # timestamp ultimo aggiustamento
        self._soglia_autotune_interval = 900      # 15 minuti tra aggiustamenti
        self._phantom_stats_snapshot = {}         # snapshot per delta calcolo
        # Stats separate per Motore 2 - ripristina da DB se disponibili
        self._m2_wins    = 0
        self._m2_losses  = 0
        self._m2_pnl     = 0.0
        self._m2_trades  = 0
        try:
            conn = sqlite3.connect(DB_PATH)
            rows = dict(conn.execute("SELECT key, value FROM bot_state WHERE key LIKE 'm2_%'").fetchall())
            conn.close()
            if rows:
                self._m2_wins   = int(rows.get('m2_wins', 0))
                self._m2_losses = int(rows.get('m2_losses', 0))
                self._m2_pnl    = float(rows.get('m2_pnl', 0.0))
                self._m2_trades = int(rows.get('m2_trades', 0))
                log.info(f"[M2_LOAD] 🧠 Stats ripristinate: {self._m2_trades}t W={self._m2_wins} L={self._m2_losses} PnL=${self._m2_pnl:.2f}")
        except Exception:
            pass
        self._m2_log     = deque(maxlen=20)   # log dedicato M2
        self._last_volume = 1.0               # ultimo volume dal WebSocket
        self._last_price  = 0.0               # ultimo prezzo dal WebSocket
        self._last_m2_heartbeat = time.time() # heartbeat M2 - monitora se il thread è vivo

        # -- SUPERCERVELLO: decisore unificato ───────────────────────────
        self.supercervello  = SuperCervello()
        self._last_sc_dec   = None
        # -- VERITAS TRACKER: chi aveva ragione ───────────────────────────
        self.veritas        = VeritatisTracker(sc_ref=self.supercervello)
        self.veritas.load(DB_PATH)  # carica statistiche dal disco al boot

        # -- ORACOLO INTERNO: sensore predittivo che vive ogni tick -------
        self._oi_carica     = 0.0        # energia accumulata 0→1
        self._oi_stato      = "ATTESA"   # ATTESA / CARICA / FUOCO
        self._oi_tick_pronto = 0         # tick consecutivi sopra soglia
        self._oi_ultimo_log  = 0.0       # timestamp ultimo log narrativo
        self._oi_narrativa   = []        # ultimi 20 messaggi narrativi
        self._oi_carica_history = []   # storia carica per grafico
        self._oi_carica_short   = 0.0  # carica ribassista speculare
        self._pred_trade_n      = 0    # predizioni confermate → trade
        self._pred_trade_pnl    = 0.0  # PnL cumulativo di quei trade
        self._oi_stato_short    = "ATTESA"
        self._oi_tick_pronto_short = 0

        # -- BRIDGE COMMANDS READER ---------------------------------------
        self._bridge_cmd_file = "bridge_commands.json"
        self._last_bridge_check = time.time()

        # -- PHANTOM TRACKER - "se avessi fatto" -------------------------
        # Traccia i trade bloccati dai 5 livelli di protezione.
        # Per ogni trade bloccato, segue il prezzo e calcola cosa sarebbe successo.
        # Zavorra o protezione? I numeri rispondono.
        self._phantoms_open = []       # trade fantasma aperti (max 5 simultanei)
        self._phantoms_closed = deque(maxlen=100)  # ultimi 100 fantasmi chiusi
        self._phantom_stats = {        # statistiche per livello di blocco
            # 'BLOCK_REASON': {'blocked': N, 'would_win': N, 'would_lose': N, 'pnl_saved': $, 'pnl_missed': $}
        }
        self._phantom_log = deque(maxlen=20)  # log dedicato fantasmi

        # -- Bridge event queue (B4) — eventi urgenti per il bridge ----
        self._bridge_event_queue = []   # lista eventi: {name, payload, ts}
        self._bridge_last_event_check = 0

        # -- Banner --------------------------------------------------------
        mode_label = "📄 PAPER TRADE" if self.paper_trade else "🔴 LIVE TRADING"
        log.info("=" * 80)
        log.info(f"🚀 OVERTOP BASSANO V15 PRODUCTION - {mode_label}")
        log.info(f"   Capital: ${self.capital:,.2f}  |  Trades totali: {self.total_trades}")
        log.info(f"   SeedScorer threshold: {SEED_ENTRY_THRESHOLD}")
        log.info(f"   Divorce triggers minimi: {DIVORCE_MIN_TRIGGERS}/4")
        log.info(f"   🎯 MOTORE 2 (Campo Gravitazionale): SHADOW ATTIVO - confronto parallelo")
        log.info("=" * 80)
        if self.paper_trade:
            log.info("⚠️  PAPER TRADE ATTIVO - nessun ordine reale verra eseguito")

    # ========================================================================
    # CONNESSIONE BINANCE WEBSOCKET
    # ========================================================================

    def connect_binance(self):
        def on_message(ws, msg):
            try:
                data   = json.loads(msg)
                price  = float(data.get('p', 0))
                volume = float(data.get('q', 1.0))
                if price > 0:
                    self.analyzer.add_price(price)
                    self.seed_scorer.add_tick(price, volume)
                    self._last_volume = volume
                    self._process_tick(price)
            except Exception as e:
                log.error(f"[WS_MSG] {e}")

        def on_error(ws, error):
            log.error(f"[WS_ERROR] {error}")

        def on_close(ws, code, msg):
            log.warning(f"[WS_CLOSE] codice={code} - riconnessione in 5s...")
            time.sleep(5)
            self.connect_binance()

        def on_open(ws):
            log.info("[WS] [OK] Connesso a Binance aggTrade SOLUSDC")

        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            on_open=on_open,
        )
        threading.Thread(target=self.ws.run_forever, daemon=True, name="ws_thread").start()

    # ========================================================================
    # PROCESS TICK - orchestratore principale
    # ========================================================================

    def _process_tick(self, price: float):
        now = time.time()

        # Config hot-reload ogni 30 s
        if now - self.last_config_check > 30:
            if self.config_reloader.check_reload():
                if self.capsule_runtime.reload():
                    caps_attive = [c.get('capsule_id','?') for c in self.capsule_runtime.capsules if c.get('enabled')]
                    log.info(f"[CAPSULE_LOAD] {len(caps_attive)} capsule ricaricate: {caps_attive[:5]}")
                    self._log_m2("💊", f"[CAPSULE_LOAD] {len(caps_attive)} capsule attive")
                    self.telemetry.log_capsule_load(caps_attive)
            self.last_config_check = now

        # Bridge commands check ogni 30 s
        if now - self._last_bridge_check > 30:
            self._read_bridge_commands()
            self._last_bridge_check = now

        # Heartbeat ogni 30 s
        if now - self.last_heartbeat > 30:
            self._update_heartbeat()
            self.last_heartbeat = now

        # Aggiorna prezzo live ad ogni tick
        self._last_price = price

        # Aggiorna prezzo live ad ogni tick (per dashboard)
        if self.heartbeat_lock:
            self.heartbeat_lock.acquire()
        try:
            if self.heartbeat_data is not None:
                self.heartbeat_data["last_price"] = round(price, 2)
                self.heartbeat_data["last_tick"]  = datetime.utcnow().isoformat()
                self.heartbeat_data["tick_count"] = self.heartbeat_data.get("tick_count", 0) + 1
                self.heartbeat_data["symbol"]     = SYMBOL
        except Exception:
            pass
        finally:
            if self.heartbeat_lock:
                self.heartbeat_lock.release()

        # Feed RegimeDetector e Decelerometer
        self.regime_detector.add_tick(price, self._last_volume)
        self.decelero.add_price(price)

        # Aggiorna regime ogni 60s
        if now - self._last_regime_check > 60:
            regime, conf, detail = self.regime_detector.detect()
            if regime != self._regime_current:
                self._log("🌍", f"REGIME → {regime} (conf={conf:.0%}) | "
                         f"trend={detail.get('trend_pct',0):+.2f}% "
                         f"dir={detail.get('dir_ratio',0):.2f}")
                ctx = self._tele_ctx()
                self.telemetry.log_regime_change(
                    self._regime_current, regime,
                    direction=ctx['direction'], open_position=ctx['open_position'],
                    active_threshold=ctx['active_threshold'], drift=ctx['drift'],
                    macd=ctx['macd'], trend=ctx['trend'], volatility=ctx['volatility'])
            self._regime_current    = regime
            self._regime_conf       = conf
            self._last_regime_check = now

        # Persistenza ogni 5 minuti
        if now - self.last_persist > 300:
            self._persist.save(self.capital, self.total_trades)
            self._persist.save_brain(self.oracolo, self.memoria, self.calibratore)
            self._persist.save_signal_tracker(self.signal_tracker)
            self._persist.save_runtime_state(self)
            self.telemetry.persist_to_db(DB_PATH)
            self.last_persist = now

        # AUTO-TUNE soglia - ciclo indipendente, il timer adattivo è interno
        self._auto_tune_soglia()

        # Calcola drift per il downgrade momentum in RANGING
        _drift_for_classify = 0.0
        if len(self.campo._prices_long) >= 100:
            _pl = list(self.campo._prices_long)
            _avg_old = sum(_pl[:50]) / 50
            _avg_new = sum(_pl[-50:]) / 50
            _drift_for_classify = (_avg_new - _avg_old) / _avg_old * 100

        # -- MOTORE 2: Feed SEMPRE — buffer prezzi deve crescere ogni tick --
        _seed_quick = self.seed_scorer.score()
        _seed_val = _seed_quick.get('score', 0.0) if _seed_quick.get('reason') != 'insufficient_data' else 0.0
        self.campo.feed_tick(price, self._last_volume, _seed_val)

        contesto = self.analyzer.analyze(regime=self._regime_current, drift=_drift_for_classify)

        # analyzer ritorna (momentum, volatility, trend) oppure (None, None, None)
        _contesto_ok = contesto[0] is not None
        _mom = contesto[0] if _contesto_ok else "MEDIO"
        _vol = contesto[1] if _contesto_ok else "MEDIA"
        _trd = contesto[2] if _contesto_ok else "SIDEWAYS"

        # Oracolo e Veritas girano sempre
        self._oracolo_interno_tick(price, _mom, _vol, _trd)
        self.veritas.aggiorna(price, time.time())
        # Salva Veritas su disco ogni 60 secondi
        if not hasattr(self, '_veritas_last_save'):
            self._veritas_last_save = 0
        if time.time() - self._veritas_last_save >= 60:
            self.veritas.save(DB_PATH)
            self._veritas_last_save = time.time()

        if not _contesto_ok:
            return
        momentum, volatility, trend = contesto
        self._last_trend = trend
        self._last_volatility = volatility
        self._last_momentum = momentum

        # -- MOTORE 2: Shadow trade evaluation (parallelo) -----------------
        if self._shadow:
            self._evaluate_shadow_exit(price, momentum, volatility, trend)
        else:
            self._evaluate_shadow_entry(price, momentum, volatility, trend)

        # -- PHANTOM TRACKER: aggiorna trade fantasma ogni tick ------------
        if self._phantoms_open:
            self._update_phantoms(price, momentum)

        # -- POST-TRADE TRACKER: monitora cosa succede dopo exit ----------
        if self.oracolo._post_trade_queue:
            self.oracolo.update_post_trade(price)

        # -- PRE-TRADE SIGNAL TRACKER: osservazione continua ogni tick ------
        # score_now() calcola senza decidere — pura mappa del segnale nel tempo.
        # Registra tutto ciò che supera 25, prima di qualsiasi filtro.
        if self.campo._tick_count > 200 and momentum:
            _seed_q = self.seed_scorer.score()
            _seed_v = _seed_q.get('score', 0.0) if _seed_q.get('reason') != 'insufficient_data' else 0.0
            _fp_wr  = self.oracolo.get_wr(momentum, volatility, trend, self.campo._direction)
            _sn     = self.campo.score_now(_seed_v, _fp_wr, momentum, volatility,
                                            trend, self._regime_current, self.campo._direction)
            if _sn['valid']:
                # Salva sempre l'ultimo score per il grafico
                self.campo._last_score  = _sn['score']
                self.campo._last_soglia = _sn['soglia']
                # Registra nel tracker se score >= 25
                # Calcola drift reale — _last_drift non esiste su campo
                _st_drift = 0.0
                if len(self.campo._prices_long) >= 100:
                    _st_p = list(self.campo._prices_long)
                    _st_old = sum(_st_p[:50]) / 50
                    _st_new = sum(_st_p[-50:]) / 50
                    if _st_old > 0:
                        _st_drift = (_st_new - _st_old) / _st_old * 100
                self.signal_tracker.record_signal(
                    price=price,
                    direction=self.campo._direction,
                    score=_sn['score'],
                    soglia=_sn['soglia'],
                    regime=self._regime_current,
                    momentum=momentum,
                    volatility=volatility,
                    trend=trend,
                    rsi=self.campo._last_rsi,
                    macd_hist=self.campo._last_macd_hist,
                    drift=round(_st_drift, 5),
                )
        # Aggiorna segnali aperti
        if self.signal_tracker.get_open_count() > 0:
            self.signal_tracker.update(price)

        # -- BRIDGE EVENTS: rate-limited — max 1 per tipo ogni 10s --------
        _now_ev = time.time()
        if len(self.campo._prices_short) >= 30:
            _pb_f, _pb_d, _pb_sigs = self.campo._pre_breakout_factor()
            if _pb_sigs >= 2:
                _last_pb = getattr(self, '_last_pb_event_ts', 0)
                if _now_ev - _last_pb >= 10:
                    _pb_payload = {'signals': _pb_sigs, 'factor': round(_pb_f, 3), 'regime': self._regime_current}
                    self._emit_bridge_event("EVENT_PREBREAKOUT", _pb_payload)
                    self.telemetry.log_event_signal("PREBREAKOUT", _pb_payload)
                    self._last_pb_event_ts = _now_ev
        if self._oi_stato == "FUOCO" and self._oi_carica >= 0.80:
            _last_fuoco = getattr(self, '_last_fuoco_event_ts', 0)
            if _now_ev - _last_fuoco >= 10:
                _fuoco_payload = {'carica': round(self._oi_carica, 3), 'regime': self._regime_current}
                self._emit_bridge_event("EVENT_FUOCO", _fuoco_payload)
                self.telemetry.log_event_signal("FUOCO", _fuoco_payload)
                self._last_fuoco_event_ts = _now_ev

        # -- HEARTBEAT M2 - ogni 60s conferma che M2 è vivo ---------------
        if now - self._last_m2_heartbeat > 60:
            self._log_m2("💓", f"M2 vivo | shadow={'aperto' if self._shadow else 'chiuso'} "
                              f"| {self._m2_trades}t W={self._m2_wins} L={self._m2_losses}")
            self._last_m2_heartbeat = now

    # ========================================================================
    # ENTRY - catena decisionale completa
    # ========================================================================

    def _log(self, emoji: str, msg: str):
        """Aggiunge una riga al log live e la spinge subito a heartbeat_data."""
        ts = datetime.utcnow().strftime('%H:%M:%S')
        entry = f"{ts} {emoji} {msg}"
        self._live_log.append(entry)
        log.info(entry)
        # Push immediato alla dashboard - non aspetta il ciclo heartbeat da 30s
        if self.heartbeat_lock:
            self.heartbeat_lock.acquire()
        try:
            if self.heartbeat_data is not None:
                self.heartbeat_data["live_log"] = list(self._live_log)
        except Exception:
            pass
        finally:
            if self.heartbeat_lock:
                self.heartbeat_lock.release()

    def _evaluate_entry(self, price, momentum, volatility, trend):

        # -- 1. SEED SCORER ------------------------------------------------
        seed = self.seed_scorer.score()
        dynamic_seed_thresh = self.calibratore.get_params()['seed_threshold']
        if not seed['pass'] or seed['score'] < dynamic_seed_thresh:
            self._log("⚡", f"SEED FAIL score={seed['score']:.3f} | {momentum}/{volatility}/{trend} @ ${price:.1f}")
            return

        # -- 2. ORACOLO DINAMICO -------------------------------------------
        is_fantasma, fantasma_reason = self.oracolo.is_fantasma(momentum, volatility, trend)
        if is_fantasma:
            self._log("👻", f"FANTASMA bloccato: {fantasma_reason}")
            return
        fingerprint_wr = self.oracolo.get_wr(momentum, volatility, trend)

        # -- 3. MATRIMONIO -------------------------------------------------
        matrimonio      = MatrimonioIntelligente.get_marriage(momentum, volatility, trend)
        matrimonio_name = matrimonio["name"]
        confidence      = matrimonio["confidence"]

        # -- 4. MEMORIA MATRIMONI ------------------------------------------
        can_enter, mem_status = self.memoria.get_status(matrimonio_name)
        if not can_enter:
            self._log("🚫", f"MEMORIA blocca {matrimonio_name}: {mem_status}")
            return

        # -- 5. CATENA 5 CAPSULE - soglie dinamiche dal calibratore ----------
        p = self.calibratore.get_params()

        allow_1, conf_1, reason_1 = self.capsule1.valida(
            fingerprint_wr, momentum, volatility, trend,
            soglia_buona=p['cap1_soglia_buona'],
            soglia_perfetta=p['cap1_soglia_perfetta'])
        if not allow_1:
            self._log("🔴", f"CAP1 COERENZA blocca | fp_wr={fingerprint_wr:.2f} {momentum}/{volatility}/{trend}")
            return

        allow_2, reason_2 = self.capsule2.riconosci(confidence)
        if not allow_2:
            self._log("🔴", f"CAP2 TRAPPOLA blocca | conf={confidence:.2f} {matrimonio_name}")
            return

        allow_3, reason_3 = self.capsule3.proteggi(
            momentum, volatility, fingerprint_wr,
            fp_minimo=p['cap3_fp_minimo'])
        if not allow_3:
            self._log("🔴", f"CAP3 PROTEZIONE blocca | {momentum}/{volatility} fp={fingerprint_wr:.2f}")
            return

        allow_4, _, reason_4 = self.capsule4.riconosci(
            fingerprint_wr, momentum, volatility,
            soglia_buona=p['cap4_soglia_buona'])

        allow_5, duration_min, reason_5 = self.capsule5.timing(
            True, allow_1, conf_1,
            conf_ok=p['cap5_conf_ok'])
        if not allow_5:
            self._log("🔴", f"CAP5 TIMING blocca | conf_1={conf_1:.2f}")
            return

        # -- 6. CAPSULE RUNTIME (JSON dinamico) ---------------------------
        ctx_caps = {
            'matrimonio':       matrimonio_name,
            'momentum':         momentum,
            'volatility':       volatility,
            'trend':            trend,
            'seed_score':       seed['score'],
            'seed_tipo':        'CONFERMATO' if seed['score'] >= 0.65 else
                                ('PROBABILE'  if seed['score'] >= SEED_ENTRY_THRESHOLD else 'IGNOTO'),
            'force':            seed['score'],
            'fingerprint_wr':   fingerprint_wr,
            'wr_oracolo':       round(fingerprint_wr * 100, 1),
            'fingerprint_n':    self.oracolo._memory.get(
                                    self.oracolo._fp(momentum, volatility, trend),
                                    {}).get('samples', 0),
            'regime':           'trending' if momentum == 'FORTE' and volatility == 'BASSA'
                                else ('choppy'  if volatility == 'ALTA'
                                else ('lateral' if momentum == 'DEBOLE' else 'normal')),
            'mode':             'PAPER' if self.paper_trade else 'LIVE',
            'loss_consecutivi': self._loss_consecutivi(),
            'ultimo_exit_type': self._last_exit_type,
            'ultima_durata':    self._last_exit_duration,
            'sample_size':      int(self.oracolo._memory.get(
                                    self.oracolo._fp(momentum, volatility, trend),
                                    {}).get('samples', 0)),
        }
        caps_check = self.capsule_runtime.valuta(ctx_caps)
        if caps_check.get('blocca'):
            self._log("💊", f"[DECISION_CHANGED_BY_CAPSULE] capsule_id={caps_check.get('reason','?')} action=blocca_entry matrimonio={matrimonio_name}")
            self.telemetry.log_capsule_load([caps_check.get('reason','?')])
            return

        # -- ENTRY CONFERMATA ----------------------------------------------
        # Position sizing continuo - funzione dell'impulso × regime
        regime_mults = self.regime_detector.get_multipliers()
        sizing = self.position_sizer.calculate(
            seed_score=seed['score'],
            fingerprint_wr=fingerprint_wr,
            confidence=confidence,
            regime_mult=regime_mults['size_mult']
        )
        # Le capsule JSON possono ancora modificare ulteriormente
        caps_size_mult = caps_check.get('size_mult', 1.0)
        if caps_size_mult != 1.0:
            self._log("💊", f"[CAPSULE_APPLY] size_mult={caps_size_mult:.2f} applicato")
        size_factor = min(PositionSizer.SIZE_MAX,
                         sizing['size_factor'] * caps_size_mult)

        self._log("🚀", f"ENTRY {matrimonio_name} | seed={seed['score']:.3f} "
                       f"fp_wr={fingerprint_wr:.2f} size={size_factor:.2f}x "
                       f"regime={self._regime_current} @ ${price:.1f}")
        self.ai_explainer.log_decision("ENTRY",
            f"Entrato in {matrimonio_name} | seed={seed['score']:.3f} "
            f"fp_wr={fingerprint_wr:.2f} size={size_factor:.2f}x regime={self._regime_current}",
            {'momentum': momentum, 'volatility': volatility, 'trend': trend,
             'seed': seed, 'fingerprint_wr': fingerprint_wr,
             'sizing': sizing, 'regime': self._regime_current})

        if not self.paper_trade:
            self._place_order("BUY", price, size_factor)

        _size_usdt_entry = round(self.capital * size_factor * 0.05, 2)  # 5% capitale × size_mult
        self.trade_open = {
            "price_entry":    price,
            "matrimonio":     matrimonio_name,
            "duration_avg":   matrimonio["duration_avg"],
            "size_mult":      size_factor,
            "size_usdt":      _size_usdt_entry,
            "direction":      self.campo._direction,
        }
        self.entry_time        = time.time()
        self.entry_momentum    = momentum
        self.entry_volatility  = volatility
        self.entry_trend       = trend
        self.entry_fingerprint = fingerprint_wr
        self.current_matrimonio= matrimonio_name
        self.max_price         = price
        self.total_trades     += 1
        self._last_entry_seed  = seed['score']    # per AutoCalibratore
        self._last_entry_fp_wr = fingerprint_wr   # per AutoCalibratore

    # ========================================================================
    # EXIT - 4 DIVORCE TRIGGERS + SMORZ + TIMEOUT
    # ========================================================================

    def _evaluate_exit(self, price, momentum, volatility, trend):
        if price > self.max_price:
            self.max_price = price

        # -- HARD STOP LOSS SUL PNL REALE - PRIORITÀ ASSOLUTA -------------
        # Stop sul PnL della posizione, NON sul prezzo BTC.
        # $1000 margine × 5x leva = $5000 esposti.
        # 1% del margine = $10 max loss per trade.
        # Il T3 drawdown 3% sul prezzo BTC è inutile: 3% BTC = ~$2100 movimento.
        # Questo stop ferma il danno PRIMA che arrivi al T3.
        exposure_m1 = self.TRADE_SIZE_USD * self.LEVERAGE
        btc_qty_m1  = exposure_m1 / self.trade_open["price_entry"]
        current_pnl_m1 = (price - self.trade_open["price_entry"]) * btc_qty_m1
        HARD_STOP_M1 = self.TRADE_SIZE_USD * 0.01  # 1% margine = $10
        if current_pnl_m1 < -HARD_STOP_M1:
            self._log("🛑", f"HARD_STOP M1 PnL=${current_pnl_m1:.1f} max=-${HARD_STOP_M1:.0f}")
            self._close_trade(price, momentum, volatility, trend,
                              reason=f"HARD_STOP_${abs(current_pnl_m1):.1f}")
            return

        # -- MOMENTUM DECELEROMETER - exit anticipata ----------------------
        # Controlla prima dei divorce triggers: se il momentum sta decelerando
        # fortemente usciamo prima che il prezzo inverta completamente
        duration = time.time() - self.entry_time
        duration_avg = self.trade_open["duration_avg"]

        if duration > duration_avg * 0.3:   # solo dopo il 30% della durata attesa
            decel = self.decelero.analyze()
            if decel['should_exit']:
                self._log("📉", f"DECEL EXIT {self.current_matrimonio} | "
                         f"decel={decel['decel_score']:.2f} "
                         f"mom_fast={decel['mom_fast']:+.4f} "
                         f"mom_slow={decel['mom_slow']:+.4f}")
                self._close_trade(price, momentum, volatility, trend,
                                  reason="DECEL_MOMENTUM")
                return

        # -- 4 DIVORCE TRIGGERS - monitorati ogni tick ---------------------
        triggers_attivi = []

        # Trigger 1: volatilita esplode (entry BASSA → ora ALTA)
        if self.entry_volatility == "BASSA" and volatility == "ALTA":
            triggers_attivi.append("T1_VOLATILITÀ_ESPLOSA")

        # Trigger 2: trend si inverte (entry UP → ora DOWN)
        if self.entry_trend == "UP" and trend == "DOWN":
            triggers_attivi.append("T2_TREND_INVERTITO")

        # Trigger 3: stop loss 2% sul PnL della posizione
        # Size tipica $500 × 2% = -$10 max per trade
        # NON sul prezzo BTC (3% di BTC = $2050 di movimento — inutile)
        _entry_price  = self.trade_open["price_entry"]
        _size_usdt    = self.trade_open.get("size_usdt", 500.0)
        _direction    = self.trade_open.get("direction", "LONG")
        if _direction == "LONG":
            _pnl_posizione = (price - _entry_price) / _entry_price * _size_usdt
        else:
            _pnl_posizione = (_entry_price - price) / _entry_price * _size_usdt
        _stop_loss_usdt = _size_usdt * 0.02  # 2% della size
        if _pnl_posizione < -_stop_loss_usdt:
            triggers_attivi.append(f"T3_STOPLOSS_PNL_{_pnl_posizione:.2f}$")
        # Mantieni anche il drawdown % come riferimento (più largo)
        drawdown_pct = ((self.max_price - price) / _entry_price) * 100

        # Trigger 4: fingerprint diverge > 50% dal valore di entry
        current_fp = self.oracolo.get_wr(momentum, volatility, trend)
        fp_diverge = abs(current_fp - self.entry_fingerprint) / max(self.entry_fingerprint, 0.001)
        if fp_diverge > DIVORCE_FP_DIVERGE_PCT:
            triggers_attivi.append(f"T4_FP_DIVERGE_{fp_diverge:.0%}")

        if len(triggers_attivi) >= DIVORCE_MIN_TRIGGERS:
            self._log("💔", f"DIVORZIO IMMEDIATO {self.current_matrimonio} | {' + '.join(triggers_attivi)}")
            self._close_trade(price, momentum, volatility, trend, reason="DIVORZIO_IMMEDIATO")
            return

        # -- SMORZ - impulso finito ----------------------------------------
        duration     = time.time() - self.entry_time
        duration_avg = self.trade_open["duration_avg"]
        # Non uscire per SMORZ se l'Oracolo vede ancora energia
        # L'Oracolo ha dimostrato di avere ragione — rispettalo fino alla fine
        _oracolo_vivo = self._oi_carica >= 0.55 or self._oi_stato in ("FUOCO", "CARICA")
        if duration > duration_avg * 0.5 and momentum == "DEBOLE" and not _oracolo_vivo:
            self._log("🌙", f"SMORZ impulso finito - {self.current_matrimonio} dopo {duration:.0f}s")
            self._close_trade(price, momentum, volatility, trend, reason="SMORZ")
            return

        # -- TIMEOUT adattivo ----------------------------------------------
        if duration > duration_avg * 3:
            self._close_trade(price, momentum, volatility, trend, reason="TIMEOUT_3X")
            return
        if duration > duration_avg and drawdown_pct > 1.0:
            self._close_trade(price, momentum, volatility, trend, reason="TIMEOUT_DD_1%")
            return

    # ========================================================================
    # CLOSE TRADE - registra, impara, aggiorna
    # ========================================================================

    def _close_trade(self, price, momentum, volatility, trend, reason: str):
        pnl    = price - self.trade_open["price_entry"]
        is_win = pnl > 0
        matrimonio_name = self.current_matrimonio
        matrimonio      = MatrimonioIntelligente.get_by_name(matrimonio_name)
        wr_expected     = matrimonio.get("wr", 0.50)

        # -- Calcola drawdown reale (per AutoCalibratore) ------------------
        if self.max_price and self.trade_open:
            drawdown_pct = ((self.max_price - price) / self.trade_open["price_entry"]) * 100
        else:
            drawdown_pct = 0.0

        # -- Aggiorna tutti i sistemi di apprendimento ---------------------
        self.oracolo.record(self.entry_momentum, self.entry_volatility, self.entry_trend, is_win)
        self.memoria.record_trade(matrimonio_name, is_win, wr_expected)
        # Tracking predizione → trade
        # Se al momento dell'entry l'Oracolo era in FUOCO/CARICA con carica > 0.5
        # il trade era guidato dalla predizione
        if self._oi_carica >= 0.5 or self._oi_stato in ("FUOCO", "CARICA"):
            self._pred_trade_n   += 1
            self._pred_trade_pnl += pnl
        # SuperCervello impara dall'esito — pesi si adattano
        if hasattr(self, '_last_sc_dec') and self._last_sc_dec:
            self.supercervello.registra_esito(self._last_sc_dec, is_win)
            self._last_sc_dec = None
        self.realtime_engine.registra_trade({'matrimonio': matrimonio_name, 'pnl': pnl, 'is_win': is_win})
        self.log_analyzer.registra({'matrimonio': matrimonio_name, 'pnl': pnl, 'is_win': is_win})
        self.realtime_engine.analizza_e_genera()

        # -- AutoCalibratore: registra osservazione ------------------------
        self.calibratore.registra_osservazione(
            seed_score=self._last_entry_seed,
            fingerprint_wr=self._last_entry_fp_wr,
            is_win=is_win,
            divorce_drawdown_usato=drawdown_pct
        )
        self._trades_since_calib += 1

        # Calibra ogni 30 trade
        if self._trades_since_calib >= 10:
            tot_now = self.wins + self.losses + (1 if is_win else 0)
            wr_now  = (self.wins + (1 if is_win else 0)) / max(1, tot_now)
            # Prima verifica se calibrazioni precedenti hanno peggiorato
            self.calibratore.inverti_se_peggiorato(wr_now)
            # Poi calibra
            modifiche = self.calibratore.calibra()
            if modifiche:
                self._log("🎯", f"AutoCalibra: {modifiche}")
            self._trades_since_calib = 0

        if is_win:
            self.wins   += 1
        else:
            self.losses += 1
        self.capital += pnl

        wr_live = (self.wins / (self.wins + self.losses) * 100) if (self.wins + self.losses) > 0 else 0
        self._log(
            "🟢" if is_win else "🔴",
            f"EXIT {matrimonio_name} {'WIN' if is_win else 'LOSS'} PnL=${pnl:+.4f} WR={wr_live:.0f}% [{reason}]"
        )
        self.ai_explainer.log_decision("EXIT",
            f"Uscito da {matrimonio_name} | PnL=${pnl:+.4f} | motivo={reason}",
            {'pnl': pnl, 'is_win': is_win, 'reason': reason})

        if not self.paper_trade:
            self._place_order("SELL", price, self.trade_open.get("size_mult", 1.0))

        # Persiste immediatamente dopo ogni trade
        self._persist.save(self.capital, self.total_trades)
        self._persist.save_brain(self.oracolo, self.memoria, self.calibratore)
        self._update_heartbeat()

        # Salva info exit per capsule reattive
        self._last_exit_type     = reason
        self._last_exit_duration = time.time() - self.entry_time if self.entry_time else 0.0

        # Reset stato trade
        self.trade_open         = None
        self.entry_time         = None
        self.entry_momentum     = None
        self.entry_volatility   = None
        self.entry_trend        = None
        self.entry_fingerprint  = None
        self.current_matrimonio = None
        self.max_price          = None

    # ========================================================================
    # STATE ENGINE - TEMPISMO
    # Non solo COSA fare, ma QUANDO NON FARLO.
    # AGGRESSIVO: soglie normali, entra liberamente
    # NEUTRO: soglie normali, entra con cautela
    # DIFENSIVO: cooldown attivo, non entra finché non si calma
    # ========================================================================

    def _state_engine_update(self, pnl, is_win, duration):
        """Chiamato DOPO ogni trade chiuso. Aggiorna lo stato."""
        now = time.time()

        # Registra trade recente
        self._m2_recent_trades.append({
            'ts': now, 'pnl': pnl, 'is_win': is_win, 'duration': duration,
            'soglia': self._shadow.get('soglia', 60) if self._shadow else 60,
            'regime': self._shadow.get('regime_entry', self._regime_current) if self._shadow else self._regime_current,
        })

        if is_win:
            self._m2_loss_streak = 0
        else:
            self._m2_loss_streak += 1
            self._m2_last_loss_time = now

            # -- COOLDOWN PROPORZIONALE AL DANNO --------------------------
            abs_pnl = abs(pnl)
            if abs_pnl < 1.0:
                base_cooldown = 10
            elif abs_pnl < 20.0:
                base_cooldown = 20
            else:
                base_cooldown = 45

            streak_mult = min(2.0, 0.5 + self._m2_loss_streak * 0.5)
            cooldown = min(120, base_cooldown * streak_mult)
            self._m2_cooldown_until = now + cooldown

        # -- TRANSIZIONE DI STATO ----------------------------------------
        # Basata su performance recente, non sul singolo trade
        old_state = self._state
        in_state_time = now - self._state_since

        # Guarda ultimi 5 trade
        recent = list(self._m2_recent_trades)[-5:]
        if len(recent) >= 3:
            recent_wins = sum(1 for t in recent if t['is_win'])
            recent_wr = recent_wins / len(recent)
            recent_pnl = sum(t['pnl'] for t in recent)

            # Solo transizioni se tempo minimo nello stato superato
            if in_state_time >= self._state_min_duration:
                if recent_wr >= 0.7 and recent_pnl > 0:
                    self._state = "AGGRESSIVO"
                elif recent_wr <= 0.3 or self._m2_loss_streak >= 3:
                    self._state = "DIFENSIVO"
                else:
                    self._state = "NEUTRO"

        if self._state != old_state:
            self._state_since = now
            self._log_m2("[CFG]️", f"STATO → {self._state} (loss_streak={self._m2_loss_streak} recent_wr={recent_wr:.0%} cooldown={self._m2_cooldown_until - now:.0f}s)")
            self.telemetry.log_state_change(old_state, self._state, self._m2_loss_streak,
                self._regime_current, self.campo._direction, self._shadow is not None)

    def _state_engine_can_enter(self) -> tuple:
        """Ritorna (can_enter: bool, reason: str). Gate PRIMA di qualsiasi entry."""
        now = time.time()

        # -- COOLDOWN ATTIVO → non entrare ------------------------------
        if now < self._m2_cooldown_until:
            remaining = self._m2_cooldown_until - now
            return False, f"COOLDOWN_{remaining:.0f}s (loss_streak={self._m2_loss_streak})"

        # -- DIFENSIVO → non entrare finché non torna NEUTRO o AGGRESSIVO
        # MA: deadlock protection - max 5 minuti in DIFENSIVO
        if self._state == "DIFENSIVO":
            time_in_defensive = now - self._state_since
            if time_in_defensive > 300:  # 5 minuti
                self._state = "NEUTRO"
                self._state_since = now
                self._m2_loss_streak = 0
                self._log_m2("[CFG]️", f"STATO → NEUTRO (auto-reset dopo {time_in_defensive/60:.1f} min in DIFENSIVO)")
                self.telemetry.log_state_change("DIFENSIVO", "NEUTRO", 0,
                    self._regime_current, self.campo._direction, self._shadow is not None)
            else:
                return False, f"DIFENSIVO_{300-time_in_defensive:.0f}s (loss_streak={self._m2_loss_streak})"

        # -- VELOCITÀ: non entrare se ultimo trade chiuso < 30 secondi fa -
        if self._m2_recent_trades:
            last = self._m2_recent_trades[-1]
            if now - last['ts'] < 30:
                return False, f"TROPPO_VELOCE ({now - last['ts']:.1f}s dall'ultimo)"

        # -- LOSS PESANTE: se ultimo loss > $50, pausa 30 secondi -----
        if self._m2_recent_trades:
            last = self._m2_recent_trades[-1]
            if not last['is_win'] and abs(last['pnl']) > 50:
                if now - last['ts'] < 30:
                    return False, f"LOSS_PESANTE_${abs(last['pnl']):.0f}_pausa"

        return True, "OK"

    # ========================================================================
    # AUTO-TUNING SOGLIA - IL SISTEMA IMPARA DAI PROPRI PHANTOM
    # Non servono manopole. I phantom dicono se la soglia è giusta.
    # ========================================================================

    def _auto_tune_soglia(self):
        """
        AUTO-TUNE ADATTIVO - intervallo e step proporzionali alla gravita.
        
        Bilancio phantom < -$500  → intervallo 120s, step 3
        Bilancio phantom < -$200  → intervallo 300s, step 2
        Bilancio phantom < -$50   → intervallo 600s, step 1
        Bilancio phantom ≥ $0     → intervallo 900s, step 1
        
        WR phantom > 75% → step × 2 (molto lontano dall'equilibrio)
        """
        now = time.time()

        phantom_summary = self._get_phantom_summary()
        bilancio = phantom_summary.get('bilancio', 0)

        if bilancio < -500:
            adaptive_interval = 120
            base_step = 3
        elif bilancio < -200:
            adaptive_interval = 300
            base_step = 2
        elif bilancio < -50:
            adaptive_interval = 600
            base_step = 1
        else:
            adaptive_interval = 900
            base_step = 1

        if now - self._last_soglia_autotune < adaptive_interval:
            return

        stats = self._phantom_stats.get("SCORE_INSUFFICIENTE")

        # FIX: AutoCalibratore guarda anche i trade reali persi consecutivi
        recent = list(self._m2_recent_trades)[-5:] if self._m2_recent_trades else []
        recent_losses = sum(1 for t in recent if not t.get('is_win', False))
        if recent_losses >= 3 and len(recent) >= 3:
            step = base_step
            new_min  = min(68, self.campo.SOGLIA_MIN  + step)
            new_base = min(72, self.campo.SOGLIA_BASE + step)
            old_min  = self.campo.SOGLIA_MIN
            old_base = self.campo.SOGLIA_BASE
            self.campo.SOGLIA_MIN  = new_min
            self.campo.SOGLIA_BASE = new_base
            self._last_soglia_autotune = now
            self._log_m2("🎯", f"AUTO-TUNE LOSS_REAL: {recent_losses}/5 loss reali → ALZA soglia "
                              f"MIN {old_min}→{new_min} BASE {old_base}→{new_base}")
            return

        if not stats:
            return

        total_closed = stats['would_win'] + stats['would_lose']
        if total_closed < 10:
            return

        prev = self._phantom_stats_snapshot.get("SCORE_INSUFFICIENTE", {})
        prev_win = prev.get('would_win', 0)
        prev_lose = prev.get('would_lose', 0)
        delta_win = stats['would_win'] - prev_win
        delta_lose = stats['would_lose'] - prev_lose
        delta_total = delta_win + delta_lose

        if delta_total < 3:
            return

        delta_wr = delta_win / delta_total

        self._phantom_stats_snapshot["SCORE_INSUFFICIENTE"] = {
            'would_win': stats['would_win'],
            'would_lose': stats['would_lose'],
        }

        old_min = self.campo.SOGLIA_MIN
        old_base = self.campo.SOGLIA_BASE

        step = base_step * 2 if delta_wr > 0.75 else base_step

        if delta_wr > 0.60:
            new_min = max(50, old_min - step)
            new_base = max(55, old_base - step)
            action = "ABBASSA"
        elif delta_wr < 0.40:
            new_min = min(68, old_min + step)
            new_base = min(72, old_base + step)
            action = "ALZA"
        elif bilancio < -100:
            # WR nella zona morta (40-60%) MA bilancio molto negativo
            # I WIN phantom sono più grossi dei LOSS → la soglia costa troppo
            # Abbassa con step ridotto (1) - cautela nella zona morta
            new_min = max(50, old_min - 1)
            new_base = max(55, old_base - 1)
            action = "ABBASSA_PNL"
        else:
            self._last_soglia_autotune = now
            self._log_m2("🎯", f"AUTO-TUNE: soglia OK (phantom WR={delta_wr:.0%} su {delta_total} campioni, bil=${bilancio:.0f})")
            return

        # Auto-tune non supera mai soglia calcolata dinamicamente
        # Il soffitto è determinato dai dati reali, non dall'algoritmo
        soglia_max_permessa = _calcola_soglia_da_signal_tracker(self)
        new_base = min(new_base, soglia_max_permessa['base'] + 5)  # max +5 rispetto al dinamico
        new_min  = min(new_min,  soglia_max_permessa['min']  + 5)
        self.campo.SOGLIA_MIN = new_min
        self.campo.SOGLIA_BASE = new_base
        self._last_soglia_autotune = now

        self._log_m2("🎯", f"AUTO-TUNE: {action} soglia step={step} | phantom WR={delta_wr:.0%} "
                          f"({delta_win}W/{delta_lose}L su {delta_total}) bil=${bilancio:.0f} "
                          f"| MIN {old_min}→{new_min} BASE {old_base}→{new_base} "
                          f"[intervallo={adaptive_interval}s]")

    # ========================================================================
    # MOTORE 2: CAMPO GRAVITAZIONALE - Shadow Entry/Exit/Close
    # ========================================================================

    def _tele_ctx(self, trend_override=None, vol_override=None, bridge_reason=None):
        """Snapshot di contesto per telemetria. Zero logica, solo lettura."""
        drift = 0.0
        if len(self.campo._prices_long) >= 100:
            _p = list(self.campo._prices_long)
            _old = sum(_p[:50]) / 50
            _new = sum(_p[-50:]) / 50
            drift = (_new - _old) / _old * 100 if _old else 0
        return {
            'regime': self._regime_current,
            'direction': self.campo._direction,
            'open_position': self._shadow is not None,
            'active_threshold': getattr(self.campo, 'SOGLIA_MAX', 0),
            'drift': drift,
            'macd': self.campo._last_macd_hist,
            'trend': trend_override or getattr(self, '_last_trend', 'UNKNOWN'),
            'volatility': vol_override or getattr(self, '_last_volatility', 'UNKNOWN'),
            'bridge_reason': bridge_reason,
        }

    def _emit_bridge_event(self, event_name: str, payload: dict):
        """
        B4: Emette evento urgente verso il bridge.
        Il bridge lo legge nel prossimo ciclo (max 5s) invece di aspettare il timer.
        """
        self._bridge_event_queue.append({
            'name': event_name,
            'payload': payload,
            'ts': time.time(),
        })
        # Mantieni solo ultimi 10 eventi
        if len(self._bridge_event_queue) > 10:
            self._bridge_event_queue.pop(0)
        # Scrivi nel heartbeat per il bridge
        if self.heartbeat_lock:
            self.heartbeat_lock.acquire()
        try:
            if self.heartbeat_data is not None:
                self.heartbeat_data['bridge_events'] = list(self._bridge_event_queue[-5:])
        except Exception:
            pass
        finally:
            if self.heartbeat_lock:
                self.heartbeat_lock.release()
        log.info(f"[BRIDGE_EVENT] {event_name} {payload}")

    def _log_m2(self, emoji: str, msg: str):
        """Log dedicato Motore 2 - separato dal Motore 1."""
        ts = datetime.utcnow().strftime('%H:%M:%S')
        entry = f"{ts} {emoji} [M2] {msg}"
        self._m2_log.append(entry)
        log.info(entry)

    def _auto_detect_direction(self, trend):
        """
        Decide automaticamente LONG o SHORT con ISTERESI + COOLDOWN + CONFERMA.
        
        ISTERESI: soglie diverse per entrare e uscire da SHORT
          - Per andare SHORT: drift < -0.12% (più lontano)
          - Per tornare LONG: drift > -0.04% (deve risalire chiaramente)
          - Zona morta tra -0.12% e -0.04%: resta dove è
        
        COOLDOWN: minimo 60 secondi tra un flip e il successivo.
        
        CONFERMA: 3 tick consecutivi con segnale bearish >=2 prima di flippare a SHORT.
                  Per tornare LONG basta 1 tick con bearish < 2 (conservativo).
        """
        campo = self.campo
        
        # Calcola drift corrente
        drift = 0.0
        if len(campo._prices_long) >= 100:
            _prices = list(campo._prices_long)
            _avg_old = sum(_prices[:50]) / 50
            _avg_new = sum(_prices[-50:]) / 50
            drift = (_avg_new - _avg_old) / _avg_old * 100
        
        macd_hist = campo._last_macd_hist
        
        # ===============================================================
        # FLIP INTELLIGENTE - non reagisce al passato, anticipa il futuro
        #
        # Il drift misura cosa È SUCCESSO. Il momentum misura cosa STA SUCCEDENDO.
        # Lo SHORT deve entrare all'INIZIO del calo, non alla fine.
        #
        # Per LONG → SHORT servono 3 condizioni SIMULTANEE:
        #   1. Momentum attuale indica calo (non solo drift passato)
        #   2. MACD conferma (histogram negativo)
        #   3. Decelerazione bassa (l'impulso ribassista è FRESCO, non esaurito)
        #
        # Per SHORT → LONG:
        #   1. Momentum non più ribassista
        #   2. Drift torna positivo O MACD gira positivo
        # ===============================================================
        
        # Analizza l'energia ribassista ATTUALE
        decel = self.decelero.analyze()
        decel_score = decel.get('decel_score', 0)
        mom_fast = decel.get('mom_fast', 0)  # momentum veloce (ultimi 5 tick)
        
        bearish_energy = 0
        
        if campo._direction == "LONG":
            # Per andare SHORT: serve impulso ribassista FRESCO
            # 1. Momentum veloce negativo (il prezzo sta scendendo ORA)
            if mom_fast < -0.5:
                bearish_energy += 1
            if mom_fast < -1.0:
                bearish_energy += 1  # impulso forte
            
            # 2. MACD conferma tendenza ribassista
            if macd_hist < -2.0:
                bearish_energy += 1
            
            # 3. Decelerazione BASSA = impulso fresco (non esaurito)
            # Se decel è alta, il calo sta gia finendo → NON flippare
            if decel_score < 0.4:
                bearish_energy += 1  # impulso ancora vivo
            
            # 4. Drift come conferma (non come trigger primario)
            if drift < -0.08:
                bearish_energy += 1
        else:
            # Per restare SHORT: basta che l'impulso ribassista non sia morto
            if mom_fast < 0:
                bearish_energy += 1
            if drift < -0.03:
                bearish_energy += 1
            if macd_hist < 0:
                bearish_energy += 1
        
        # Conferma: conta tick consecutivi con energia bearish alta
        if bearish_energy >= 3:
            campo._direction_bearish_streak += 1
        else:
            campo._direction_bearish_streak = 0
        
        # Cooldown: minimo 120 secondi tra flip (non 60 - troppo nervoso)
        now = time.time()
        cooldown_ok = (now - campo._direction_last_change) >= 120
        
        old_direction = campo._direction
        
        # -- EXPLOSIVE GATE: in EXPLOSIVE flip SHORT con meno energia ------
        # Signal Tracker: SHORT EXPLOSIVE hit 89% su 36 segnali — gate permissivo
        if self._regime_current == "EXPLOSIVE" and campo._direction == "LONG" and bearish_energy >= 2 and cooldown_ok:
            campo._direction = "SHORT"
            campo._direction_last_change = now
            campo._direction_bearish_streak = 0
            self._log_m2("🔄", f"FLIP → SHORT in EXPLOSIVE (bearish_energy={bearish_energy} drift={drift:+.3f}%)")

        # -- RANGING GATE: in laterale NON flippare a SHORT --------------
        # ECCEZIONE VERITAS: se il Veritas vede movimento ribassista reale
        # con delta_60s < -20 su almeno 5 segnali → lo SHORT è legittimo
        _veritas_short_ok = False
        _drift_short_ok = drift < -0.005 and bearish_energy >= 3
        if hasattr(self, 'veritas') and self.veritas._stats:
            for k, s in self.veritas._stats.items():
                if 'FUOCO' in k or 'CARICA' in k:
                    deltas = s.get('deltas', [])
                    if len(deltas) >= 5:
                        avg_delta = sum(deltas) / len(deltas)
                        if avg_delta < -20:  # prezzo scende consistentemente
                            _veritas_short_ok = True
                            break

        if self._regime_current == "RANGING" and campo._direction == "LONG" and campo._direction_bearish_streak >= 3 and cooldown_ok and not _veritas_short_ok and not _drift_short_ok:
            # NON flippare - logga come SHORT evitato
            if not hasattr(self, '_shadow_short_log'):
                self._shadow_short_log = []
            if not hasattr(self, '_shadow_short_phantoms'):
                self._shadow_short_phantoms = []       # phantom SHORT aperti
            if not hasattr(self, '_shadow_short_results'):
                self._shadow_short_results = deque(maxlen=100)  # risultati chiusi
            
            current_price = self._last_price if hasattr(self, '_last_price') else 0
            
            self._shadow_short_log.append({
                'ts': now,
                'drift': drift,
                'macd_hist': macd_hist,
                'bearish_energy': bearish_energy,
                'mom_fast': decel.get('mom_fast', 0),
                'decel_score': decel_score,
                'regime': self._regime_current,
                'price': current_price,
            })
            
            # Apri phantom SHORT per simulare l'outcome
            if len(self._shadow_short_phantoms) < 3 and current_price > 0:
                self._shadow_short_phantoms.append({
                    'price_entry': current_price,
                    'entry_time': now,
                    'drift': drift,
                    'macd_hist': macd_hist,
                    'bearish_energy': bearish_energy,
                    'max_price': current_price,
                    'min_price': current_price,
                })
            
            self._log_m2("🔇", f"SHORT EVITATO in RANGING (drift={drift:+.3f}% macd={macd_hist:+.2f} energy={bearish_energy})")
            campo._direction_bearish_streak = 0
            # Non flippa - resta LONG
        
        # In NON-RANGING: flip normale LONG → SHORT
        elif campo._direction == "LONG" and campo._direction_bearish_streak >= 3 and cooldown_ok:
            campo._direction = "SHORT"
            campo._direction_last_change = now
            campo._direction_bearish_streak = 0
        # SHORT → LONG: energia bearish scesa sotto 2 + cooldown
        elif campo._direction == "SHORT" and bearish_energy < 2 and cooldown_ok:
            campo._direction = "LONG"
            campo._direction_last_change = now
            campo._direction_bearish_streak = 0
        
        # SHORT in RANGING: mantenuto se il drift è negativo
        # Non forzare LONG quando il mercato scende
        
        if campo._direction != old_direction:
            self._log_m2("🔄", f"DIREZIONE → {campo._direction} (drift={drift:+.3f}% macd_hist={macd_hist:+.2f} trend={trend})")
            self.telemetry.log_direction_flip(
                old_direction, campo._direction,
                regime=self._regime_current, direction=campo._direction,
                open_position=self._shadow is not None,
                active_threshold=getattr(campo, 'SOGLIA_MAX', 0),
                drift=drift, macd=macd_hist, trend=trend,
                volatility=getattr(self, '_last_volatility', 'UNKNOWN'))
        else:
            self.telemetry.log_direction_hold(
                bearish_energy,
                regime=self._regime_current, direction=campo._direction,
                open_position=self._shadow is not None,
                active_threshold=getattr(campo, 'SOGLIA_MAX', 0),
                drift=drift, macd=macd_hist, trend=trend,
                volatility=getattr(self, '_last_volatility', 'UNKNOWN'))

    def _get_signal_tracker_context(self, regime: str, score: float) -> dict:
        """Legge il contesto rilevante dal Signal Tracker per il SuperCervello."""
        if score >= 75:   band = "FORTE_75+"
        elif score >= 65: band = "BUONO_65-75"
        elif score >= 58: band = "BASE_58-65"
        else:             band = "DEBOLE_<58"
        key = f"{regime}|LONG|{band}"
        stats = getattr(self.signal_tracker, '_stats', {}).get(key, {})
        hits = stats.get('hit_60', [])
        pnls = stats.get('pnl_sim', [])
        n = len(hits)
        return {
            'hit_rate': sum(hits)/n if n>0 else 0.5,
            'pnl_sim':  sum(pnls)/len(pnls) if pnls else 0.0,
            'n': n,
        }

    def _oracolo_interno_tick(self, price: float, momentum: str,
                              volatility: str, trend: str):
        """
        Oracolo predittivo interno — vive ogni tick grezzo.
        
        Calcola le feature sequenziali sul buffer prezzi reale,
        accumula la carica tick per tick, e genera narrativa
        che racconta il momento presente — non il passato.
        
        NON decide l'entry — prepara il terreno per _evaluate_shadow_entry.
        La narrativa viene esposta nel heartbeat per la dashboard.
        """
        now = time.time()

        # Feature sequenziali sul buffer prezzi reale
        pp = list(self.campo._prices_short)
        dd = []
        if len(self.campo._prices_long) >= 20:
            pl = list(self.campo._prices_long)[-20:]
            dd = [pl[i]-pl[i-1] for i in range(1, len(pl))]

        if len(pp) < 20 or len(dd) < 10:
            return

        r5  = max(pp[-5:])  - min(pp[-5:])
        r10 = max(pp[-10:]) - min(pp[-10:])
        r20 = max(pp[-20:]) - min(pp[-20:])

        if r20 == 0:
            return

        # L1: geometria
        pos     = (pp[-1] - min(pp[-20:])) / r20
        cr      = r5 / (r10 + 0.01)
        midzone = 0.40 <= pos <= 0.60
        bordo   = pos >= 0.80

        # L2: sequenza
        dp = sum(1 for d in dd[-10:] if d > 0) / 10
        sf = sum(1 for i in range(1, len(dd)) if dd[i]*dd[i-1] < 0)
        ds = sum(dd[-5:])/5 - sum(dd[-15:])/15 if len(dd) >= 15 else 0

        # Volume pressure dal seed scorer
        _sv = self.seed_scorer.score()
        vp  = _sv.get('vol_accel', 0.5) + 1.0 if _sv.get('reason') != 'insufficient_data' else 1.0

        # L3: memoria dal Signal Tracker
        _mem_hit = 0.5
        if hasattr(self, 'signal_tracker'):
            stats = getattr(self.signal_tracker, '_stats', {})
            regime_key = f"{self._regime_current}|LONG"
            for k, v in stats.items():
                if regime_key in k:
                    hits = v.get('hit_60', [])
                    if len(hits) >= 5:
                        _mem_hit = sum(hits) / len(hits)
                        break

        # Calcola nuova carica
        nc = 0.0
        if not midzone:
            if cr   < 0.80: nc += 0.20
            if bordo:        nc += 0.25
            if dp  >= 0.60: nc += 0.20
            if sf  <= 4:    nc += 0.15
            if vp  >= 1.1:  nc += 0.15
            if ds  >  0:    nc += 0.05
            if _mem_hit >= 0.65: nc = min(1.0, nc * 1.20)
        else:
            nc = 0.0  # midzone: carica si azzera

        self._oi_carica = self._oi_carica * 0.75 + nc * 0.25
        self._oi_carica_history.append(round(self._oi_carica, 3))
        if len(self._oi_carica_history) > 200: self._oi_carica_history.pop(0)

        # Stato macchina LONG
        vecchio_stato = self._oi_stato
        if midzone:
            self._oi_stato      = "ATTESA"
            self._oi_tick_pronto = 0
        elif self._oi_carica >= 0.65:
            self._oi_tick_pronto += 1
            self._oi_stato = "FUOCO" if self._oi_tick_pronto >= 2 else "CARICA"
        elif self._oi_carica >= 0.40:
            self._oi_stato      = "CARICA"
            self._oi_tick_pronto = 0
        else:
            self._oi_stato      = "ATTESA"
            self._oi_tick_pronto = 0

        # ── CARICA SHORT speculare ────────────────────────────────────
        # Stessa logica ma invertita: bordo inferiore, drift negativo
        if len(pp) >= 20:
            pos_short  = 1.0 - pos            # bordo inferiore
            dp_short   = 1.0 - dp             # drift persistente negativo
            ds_short   = -ds if ds < 0 else 0 # drift accelera verso il basso
            bordo_short = pos_short >= 0.80

            nc_short = 0.0
            if not midzone:
                if cr       < 0.80:   nc_short += 0.20
                if bordo_short:       nc_short += 0.25
                if dp_short >= 0.60:  nc_short += 0.20
                if sf       <= 4:     nc_short += 0.15
                if vp       >= 1.1:   nc_short += 0.15
                if ds_short > 0:      nc_short += 0.05

            self._oi_carica_short = self._oi_carica_short * 0.75 + nc_short * 0.25

            # Stato SHORT
            vecchio_short = self._oi_stato_short
            if midzone:
                self._oi_stato_short = "ATTESA"
                self._oi_tick_pronto_short = 0
            elif self._oi_carica_short >= 0.65:
                self._oi_tick_pronto_short += 1
                self._oi_stato_short = "FUOCO" if self._oi_tick_pronto_short >= 2 else "CARICA"
            elif self._oi_carica_short >= 0.40:
                self._oi_stato_short = "CARICA"
                self._oi_tick_pronto_short = 0
            else:
                self._oi_stato_short = "ATTESA"
                self._oi_tick_pronto_short = 0

            # Registra FUOCO SHORT nel Veritas
            if self._oi_stato_short == "FUOCO" and vecchio_short != "FUOCO":
                if hasattr(self, 'veritas'):
                    _p = list(self.campo._prices_short)[-1] if self.campo._prices_short else 0
                    self.veritas.registra(
                        price=_p,
                        oi_stato="FUOCO_SHORT",
                        oi_carica=self._oi_carica_short,
                        sc_decisione="BLOCCA",  # default blocca fino a verifica
                        sc_confidenza=0.5,
                        regime=self._regime_current,
                        ts=time.time()
                    )

        # VERITAS: registra ogni transizione a FUOCO o ogni CARICA >= 0.60
        # Non aspetta lo score — registra il segnale fisico e misura la verità a 60s
        if hasattr(self, 'veritas'):
            if self._oi_stato == "FUOCO" and vecchio_stato != "FUOCO":
                # Nuova transizione a FUOCO — registra immediatamente
                sc_dec = "SCONOSCIUTO"
                sc_conf = 0.0
                if hasattr(self, 'supercervello'):
                    # Stima decisione SC con dati correnti
                    sc_conf = self.supercervello._pesi.get('campo_carica', 0.2)
                    sc_dec = "PREVISTO_ENTRA" if self._oi_carica >= 0.70 else "PREVISTO_CARICA"
                self.veritas.registra(
                    price=price,
                    oi_stato=self._oi_stato,
                    oi_carica=self._oi_carica,
                    sc_decisione=sc_dec,
                    sc_confidenza=sc_conf,
                    regime=self._regime_current,
                    ts=time.time()
                )
            elif self._oi_stato == "CARICA" and self._oi_carica >= 0.55:
                # Carica alta — registra anche senza FUOCO completo
                if not hasattr(self, '_veritas_last_carica_ts'):
                    self._veritas_last_carica_ts = 0
                if time.time() - self._veritas_last_carica_ts >= 30:
                    self._veritas_last_carica_ts = time.time()
                    self.veritas.registra(
                        price=price,
                        oi_stato="CARICA",
                        oi_carica=self._oi_carica,
                        sc_decisione="ATTESA_SC",
                        sc_confidenza=0.0,
                        regime=self._regime_current,
                        ts=time.time()
                    )

        # Narrativa — aggiorna ogni 2 secondi max
        if now - self._oi_ultimo_log >= 2.0:
            self._oi_ultimo_log = now
            msg = self._oi_narrativa_tick(
                pos, cr, dp, sf, vp, ds, _mem_hit, midzone, vecchio_stato)
            if msg:
                self._oi_narrativa.append(f"{datetime.utcnow().strftime('%H:%M:%S')} {msg}")
                if len(self._oi_narrativa) > 20:
                    self._oi_narrativa.pop(0)

        # Registra nel Veritas ogni volta che l'Oracolo scatta FUOCO
        if self._oi_stato == "FUOCO" and vecchio_stato != "FUOCO":
            # Nuovo FUOCO — registra per verifica 60s
            sc_dec = getattr(self, '_last_sc_dec', None)
            sc_decisione = "ENTRA" if sc_dec and sc_dec.get('azione')=="ENTRA" else "BLOCCA"
            sc_conf = sc_dec.get('confidenza', 0.5) if sc_dec else 0.5
            if hasattr(self, 'veritas'):
                self.veritas.registra(
                    price=list(self.campo._prices_short)[-1] if self.campo._prices_short else 0,
                    oi_stato="FUOCO",
                    oi_carica=self._oi_carica,
                    sc_decisione=sc_decisione,
                    sc_confidenza=sc_conf,
                    regime=self._regime_current,
                    ts=time.time()
                )

        # Esponi nel heartbeat
        if self.heartbeat_lock:
            self.heartbeat_lock.acquire()
        try:
            if self.heartbeat_data is not None:
                self.heartbeat_data["oi_stato"]       = self._oi_stato
                self.heartbeat_data["oi_carica"]      = round(self._oi_carica, 3)
                self.heartbeat_data["oi_stato_short"] = self._oi_stato_short
                self.heartbeat_data["oi_carica_short"]= round(self._oi_carica_short, 3)
                self.heartbeat_data["oi_narrativa"] = self._oi_narrativa[-5:]
                # Storia prezzi e carica per grafico SC — ultimi 120 tick
                _ph = list(self.campo._prices_short)[-120:]
                self.heartbeat_data["sc_price_history"] = [round(p,2) for p in _ph]
                _ch = list(self._oi_carica_history)[-120:] if hasattr(self,'_oi_carica_history') else []
                self.heartbeat_data["sc_carica_history"] = _ch

                # Metriche predizione vs mercato reale
                if len(_ph) >= 10 and len(_ch) >= 10:
                    # Predizione dai delta reali del Veritas — non fattore inventato
                    # Usa il delta medio misurato per ogni livello di carica
                    _vt_stats = self.veritas._stats if hasattr(self.veritas, '_stats') else {}
                    _delta_fuoco  = 0.0
                    _delta_carica = 0.0
                    _n_fuoco = 0
                    for k, s in _vt_stats.items():
                        if 'FUOCO' in k and s.get('n', 0) >= 5:
                            deltas = s.get('deltas', [])
                            if deltas:
                                _delta_fuoco += sum(deltas) / len(deltas)
                                _n_fuoco += 1
                        elif 'CARICA' in k and s.get('n', 0) >= 5:
                            deltas = s.get('deltas', [])
                            if deltas:
                                _delta_carica += sum(deltas) / len(deltas)
                    if _n_fuoco > 0:
                        _delta_fuoco /= _n_fuoco
                    # Fallback se Veritas non ha ancora dati sufficienti
                    if _delta_fuoco == 0 and _ph:
                        _vt_closed = self.veritas._closed[-200:] if self.veritas._closed else []
                        _fuoco_d = [s['delta_60'] for s in _vt_closed if s.get('oi_carica',0)>=0.65 and 'delta_60' in s]
                        _carica_d = [s['delta_60'] for s in _vt_closed if 0.40<=s.get('oi_carica',0)<0.65 and 'delta_60' in s]
                        if len(_fuoco_d) >= 3: _delta_fuoco = sum(_fuoco_d) / len(_fuoco_d)
                        if len(_carica_d) >= 3: _delta_carica = sum(_carica_d) / len(_carica_d)
                    # Predizione: prezzo + delta atteso in base alla carica
                    preds = []
                    for i in range(min(len(_ph), len(_ch))):
                        c = _ch[i]
                        _price_ref = _ph[i] if _ph[i] > 0 else 100.0
                        if c >= 0.65:
                            delta = _delta_fuoco if _delta_fuoco != 0 else 0.0
                        elif c >= 0.40:
                            delta = _delta_carica if _delta_carica != 0 else 0.0
                        else:
                            delta = 0.0
                        preds.append(round(_ph[i] + delta, 2))
                    # Scostamento medio assoluto
                    scost = [abs(preds[i] - _ph[i]) for i in range(len(preds))]
                    scost_avg = round(sum(scost) / len(scost), 2)
                    # Conferme: predizione indicava direzione giusta?
                    conferme = 0
                    totale = 0
                    # Soglia = 10% del delta medio della predizione
                    # Se _delta_fuoco=0 (Veritas non ancora pronto) → soglia basata su movimento tick
                    _pred_range = max(abs(preds[i] - preds[i-1]) for i in range(1, len(preds))) if len(preds) > 1 else 0
                    _sig_threshold = max(_pred_range * 0.10, _ph[0] * 0.00005) if _ph and _pred_range > 0 else max(0.05, _ph[0] * 0.00005) if _ph else 0.05
                    for i in range(1, len(preds)):
                        dir_pred   = preds[i] - preds[i-1]
                        dir_reale  = _ph[i]   - _ph[i-1]
                        if abs(dir_pred) > _sig_threshold:    # solo segnali significativi
                            totale += 1
                            if (dir_pred > 0) == (dir_reale > 0):
                                conferme += 1
                    conf_pct = round(conferme / totale * 100, 1) if totale > 0 else 0
                    self.heartbeat_data["pred_scostamento"] = scost_avg
                    self.heartbeat_data["pred_conferme"]    = conferme
                    self.heartbeat_data["pred_totale"]      = totale
                    self.heartbeat_data["pred_score"]       = conf_pct
                    self.heartbeat_data["pred_trade_n"]     = self._pred_trade_n
                    self.heartbeat_data["pred_trade_pnl"]   = round(self._pred_trade_pnl, 2)
                    self.heartbeat_data["pred_delta_fuoco"]  = round(_delta_fuoco, 4)
                    self.heartbeat_data["pred_delta_carica"] = round(_delta_carica, 4)

                    # Ratio magnitudine: predizione vs movimento reale
                    # Misura quanto la predizione sovra/sottostima il mercato
                    movimenti_pred  = []
                    movimenti_reali = []
                    # Soglia adattiva — proporzionale al movimento reale, non al prezzo assoluto
                    _dp_thresh_mag = max(0.01, (_ph[0] * 0.00003)) if _ph else 0.01
                    for i in range(1, min(len(preds), len(_ph))):
                        dp = preds[i] - preds[i-1]
                        dr = _ph[i]   - _ph[i-1]
                        if abs(dp) > _dp_thresh_mag and abs(dr) > 0.001:
                            movimenti_pred.append(abs(dp))
                            movimenti_reali.append(abs(dr))

                    if movimenti_pred and movimenti_reali:
                        avg_pred  = sum(movimenti_pred)  / len(movimenti_pred)
                        avg_reale = sum(movimenti_reali) / len(movimenti_reali)
                        ratio = round(avg_reale / avg_pred * 100, 1) if avg_pred > 0 else 100.0
                        # Fattore di correzione — usato per calibrare la magnitudine
                        # < 100% = predizione troppo aggressiva
                        # > 100% = predizione troppo conservativa
                        # 100% = perfettamente calibrata
                        if not hasattr(self, '_pred_ratio_history'):
                            self._pred_ratio_history = []
                        self._pred_ratio_history.append(ratio)
                        if len(self._pred_ratio_history) > 50:
                            self._pred_ratio_history.pop(0)
                        ratio_smooth = round(
                            sum(self._pred_ratio_history) / len(self._pred_ratio_history), 1)
                        self.heartbeat_data["pred_ratio"]     = ratio_smooth
                        self.heartbeat_data["pred_ratio_raw"] = ratio
                        # Aggiorna SC per boost predizione e Veritas stats
                        if hasattr(self, 'supercervello'):
                            self.supercervello._pred_score_ref = conf_pct
                            self.supercervello._pred_calib_ref = ratio_smooth
                            self.supercervello._veritas_stats_ref = self.veritas._stats

                # FIX: _ctx aggiornato SEMPRE ad ogni tick dell'Oracolo Interno,
                # indipendentemente da movimenti_pred/supercervello.
                # drift calcolato realmente da _prices_long (non _last_drift che non esiste).
                _ia_drift_ctx = 0.0
                if len(self.campo._prices_long) >= 100:
                    _p_ctx = list(self.campo._prices_long)
                    _avg_old_ctx = sum(_p_ctx[:50]) / 50
                    _avg_new_ctx = sum(_p_ctx[-50:]) / 50
                    if _avg_old_ctx > 0:
                        _ia_drift_ctx = (_avg_new_ctx - _avg_old_ctx) / _avg_old_ctx * 100
                self.realtime_engine._ctx = {
                    'sc_pesi': self.supercervello._pesi.copy() if hasattr(self, 'supercervello') else {},
                    'oi_carica': self._oi_carica,
                    'oi_stato': self._oi_stato,
                    'drift': round(_ia_drift_ctx, 5),
                    'macd_hist': self.campo._last_macd_hist,
                    'regime': self._regime_current,
                    'signal_tracker_stats': self.signal_tracker._stats,
                    'veritas_stats': self.veritas._stats,
                    'phantom_stats': self._phantom_stats,
                }
                # Pesi SuperCervello
                if hasattr(self,'supercervello'):
                    self.heartbeat_data["sc_pesi"] = self.supercervello._pesi
                # Veritas dashboard
                self.heartbeat_data["veritas"] = self.veritas.dump_dashboard()
        except Exception:
            pass
        finally:
            if self.heartbeat_lock:
                self.heartbeat_lock.release()

    def _oi_narrativa_tick(self, pos, cr, dp, sf, vp, ds,
                            mem_hit, midzone, vecchio_stato) -> str:
        """Genera narrativa che racconta il momento fisico presente."""
        carica = self._oi_carica
        stato  = self._oi_stato

        # Transizioni di stato — eventi importanti
        if vecchio_stato != stato:
            if stato == "FUOCO":
                return f"🚀 FUOCO — carica {carica:.2f} confermata. Bordo {pos:.0%}, compressione {cr:.2f}, drift persistente {dp:.0%}"
            elif stato == "CARICA" and vecchio_stato == "ATTESA":
                return f"⚡ Carica {carica:.2f} — molla si carica"
            elif stato == "ATTESA" and vecchio_stato in ("CARICA","FUOCO"):
                return f"💤 Energia cade {carica:.2f} — aspetto"

        # Narrativa continua per stato CARICA
        if stato == "CARICA" and carica >= 0.50:
            parts = []
            if cr < 0.70:    parts.append(f"compressione {cr:.2f}")
            if dp >= 0.70:   parts.append(f"drift {dp:.0%}")
            if vp >= 1.2:    parts.append(f"volume +{(vp-1)*100:.0f}%")
            if mem_hit>=0.65: parts.append(f"memoria {mem_hit:.0%}")
            if parts:
                return f"⚡ Carica {carica:.2f} — {', '.join(parts)}"

        # Midzone
        if midzone and vecchio_stato != "ATTESA":
            return f"🚫 Midzone pos={pos:.0%} — zero trade"

        # FUOCO attivo — segue la posizione
        if stato == "FUOCO":
            return f"🔥 Carica {carica:.2f} — energia viva"

        return ""

    def _evaluate_shadow_entry(self, price, momentum, volatility, trend):
        """Motore 2 valuta entry con il Campo Gravitazionale."""
        try:
            # -- DRIFT REALE calcolato una sola volta per tick ----------------
            # _last_drift non esiste su campo — calcoliamo da _prices_long
            _drift_real = 0.0
            if len(self.campo._prices_long) >= 100:
                _dp = list(self.campo._prices_long)
                _d_old = sum(_dp[:50]) / 50
                _d_new = sum(_dp[-50:]) / 50
                if _d_old > 0:
                    _drift_real = (_d_new - _d_old) / _d_old * 100

            # -- STATE ENGINE GATE - PRIMA DI TUTTO -----------------------
            can_enter, gate_reason = self._state_engine_can_enter()
            if not can_enter:
                # Non logga phantom per cooldown - è silenzio voluto, non opportunita
                return

            # -- ANTI-DUPLICATE: un solo entry per tick ----------------
            # Evita che VERITAS_FUOCO generi 7 entry nello stesso secondo
            _now_tick = round(time.time(), 1)  # risoluzione 0.1s
            if getattr(self, '_last_entry_tick', 0) == _now_tick:
                return
            self._last_entry_tick = _now_tick


            seed = self.seed_scorer.score()
            if seed.get('reason') == 'insufficient_data':
                return

            # -- GATE: cespuglio avvelenato — entry deboli RANGING con loss streak --
            # BYPASS: se l'Oracolo conosce questo fingerprint con WR>=60% su 8+ campioni
            # il pattern è statisticamente vincente — bypassa il blocco
            if self._regime_current == "RANGING":
                _recent = list(self._m2_recent_trades)[-3:]
                _loss_deboli = sum(1 for t in _recent
                    if not t.get('is_win')
                    and t.get('soglia', 60) < 58
                    and t.get('regime', '') == 'RANGING')
                if _loss_deboli >= 2:
                    _score_now = getattr(self.campo, '_last_score', 0)
                    if _score_now < 58:
                        # BYPASS: controlla se l'Oracolo conosce questo fingerprint come vincente
                        _fp_wr_now = self.oracolo.get_wr(momentum, volatility, trend, self.campo._direction)
                        _fp_samples = self.oracolo._memory.get(
                            f"{momentum}|{volatility}|{trend}|{self.campo._direction}", {}
                        ).get('samples', 0)
                        # Controlla Signal Tracker — itera su tutte le key per regime+direzione
                        _dir_now = self.campo._direction
                        _st_all = getattr(self.signal_tracker, '_stats', {})
                        _st_hit_rate = 0.0
                        _st_n = 0
                        for _sk, _sv in _st_all.items():
                            if self._regime_current in _sk and _dir_now in _sk:
                                _sh = list(_sv.get('hit_60', []) or [])
                                if len(_sh) >= 10:
                                    _st_hit_rate = sum(_sh)/len(_sh)
                                    _st_n = len(_sh)
                                    break
                        _st_bypass = _st_hit_rate >= 0.60 and _st_n >= 10 and not is_absolute and not is_absolute
                        if (_fp_wr_now >= 0.60 and _fp_samples >= 5) or _st_bypass:
                            _motivo = f"ST hit={_st_hit_rate:.0%} n={_st_n}" if _st_bypass else f"Oracolo WR={_fp_wr_now:.0%} n={_fp_samples:.0f}"
                            self._log_m2("✅", f"CESPUGLIO bypass — {_motivo} su {momentum}|{volatility}|{trend} — entro")
                        else:
                            if self._oi_stato == "FUOCO" and self._oi_carica >= 0.65 and getattr(self, "_last_fingerprint_wr", 0) >= 0.55:
                                self._log_m2("🔥", f"CESPUGLIO bypassed — FUOCO carica={self._oi_carica:.2f}")
                            else:
                                self._log_m2("🚫", f"CESPUGLIO_AVVELENATO: {_loss_deboli} loss deboli RANGING "
                                                  f"score={_score_now:.1f}<58 — attendo segnale forte")
                                if len(self._phantoms_open) < 5:
                                    self._record_phantom(price, f"CESPUGLIO_RANGING_{_loss_deboli}loss",
                                        seed.get('score', 0), momentum, volatility, trend)
                                return

            # -- CAPSULE 1-5: stessa protezione del Motore 1 --------------
            # M2 usa le stesse capsule di M1 per non entrare in matrimoni tossici.
            # Capsule2 blocca confidence < 0.50 → RANGE_DEAD (conf=0.30) bloccato.
            _mat_m2   = MatrimonioIntelligente.get_marriage(momentum, volatility, trend)
            _conf_m2  = _mat_m2.get('confidence', 0.5)
            # Soglia abbassata a 0.35 — dati phantom: conf 0.40 ha net +$924
            _cap2_soglia = getattr(self, '_cap2_soglia_override', 0.30)
            _allow2, _reason2 = self.capsule2.riconosci(_conf_m2) if _conf_m2 >= _cap2_soglia else (True, "OK")
            if not _allow2:
                if self._oi_stato == "FUOCO" and self._oi_carica >= 0.65 and getattr(self, "_last_fingerprint_wr", 0) >= 0.55:
                    self._log_m2("🔥", f"CAP2 bypassed — FUOCO carica={self._oi_carica:.2f}")
                else:
                    if len(self._phantoms_open) < 5:
                        self._record_phantom(price, f"CAP2_M2_{_mat_m2['name']}_conf{_conf_m2:.2f}",
                            seed['score'], momentum, volatility, trend)
                    return

            # -- DECIDI DIREZIONE: LONG o SHORT -----------------------------
            # Il mercato decide, non noi. Drift + MACD + Trend = verdetto.
            self._auto_detect_direction(trend)

            _dir = self.campo._direction
            fingerprint_wr = self.oracolo.get_wr(momentum, volatility, trend, _dir)
            self._last_fingerprint_wr = fingerprint_wr
            self._last_fingerprint_wr = fingerprint_wr  # salvato per bypass FUOCO precedenti

            # -- GATE EXPLOSIVE: entra solo con fingerprint provato ---------
            # In EXPLOSIVE la volatilità è massima — serve evidenza storica forte
            # Senza memoria reale su questo pattern, il rischio è troppo alto
            if self._regime_current == "EXPLOSIVE":
                _exp_mem = self.oracolo._memory.get(
                    f"{momentum}|{volatility}|{trend}|{_dir}", {})
                _exp_real = _exp_mem.get('real_samples', 0)
                _exp_wr   = fingerprint_wr
                if _exp_real < 5 or _exp_wr < 0.35:
                    self._record_phantom(price, f"EXPLOSIVE_GATE_fp={_exp_wr:.0%}_n={_exp_real}",
                        seed['score'], momentum, volatility, trend)
                    return

            matrimonio     = MatrimonioIntelligente.get_marriage(momentum, volatility, trend)
            matrimonio_name = matrimonio["name"]
            fantasma_info  = self.oracolo.is_fantasma(momentum, volatility, trend, _dir)

            result = self.campo.evaluate(
                seed_score=seed['score'],
                fingerprint_wr=fingerprint_wr,
                momentum=momentum,
                volatility=volatility,
                trend=trend,
                regime=self._regime_current,
                matrimonio_name=matrimonio_name,
                divorzio_set=self.memoria.divorzio,
                fantasma_info=fantasma_info,
                loss_consecutivi=self._m2_loss_consecutivi(),
                soglia_boost=self._get_ia_soglia_boost(momentum, volatility, trend),
            )

            if result['veto']:
                veto = result['veto']
                # TOSSICO e DIVORZIO sono assoluti — FUOCO non li bypassa mai
                is_absolute = (veto.startswith("TOSSICO") or 
                               veto.startswith("CM_TOSSICO") or
                               veto.startswith("STATIC_TOSSICO") or
                               veto.startswith("DIVORZIO"))
                if not is_absolute and self._oi_stato == "FUOCO" and self._oi_carica >= 0.65 and getattr(self, "_last_fingerprint_wr", 0) >= 0.55:
                    self._log_m2("🔥", f"VETO bypassed — FUOCO carica={self._oi_carica:.2f} veto={veto}")
                else:
                    if not veto.startswith("WARMUP") and len(self._phantoms_open) < 5:
                        self._record_phantom(price, veto, seed['score'], momentum, volatility, trend)
                    return

            if not result['enter']:
                # FUOCO BYPASS — forza entry con size ridotta
                # NON in EXPLOSIVE: troppo volatile, FUOCO non è affidabile
                _fuoco_bypass_ok = (self._oi_stato == "FUOCO" and 
                                    self._oi_carica >= 0.65 and 
                                    getattr(self, "_last_fingerprint_wr", 0) >= 0.55 and
                                    self._regime_current != "EXPLOSIVE")
                if _fuoco_bypass_ok:
                    self._log_m2("🔥", f"ENTER FORCED — FUOCO carica={self._oi_carica:.2f} score={result['score']:.1f}")
                    result['enter'] = True
                    result['size'] = min(result.get('size', 1.0), 0.3)
                else:
                    _rf_wr = self.oracolo.get_wr(momentum, volatility, trend, self.campo._direction)
                    _rf_mem = self.oracolo._memory.get(
                        f"{momentum}|{volatility}|{trend}|{self.campo._direction}", {}
                    )
                    _rf_samples = _rf_mem.get('samples', 0)
                    _rf_real = _rf_mem.get('real', 0)

                    if (self._regime_current == "RANGING" and
                        _rf_wr >= 0.60 and _rf_samples >= 5 and
                        result['score'] >= 40 and
                        not result['veto']):
                        self._log_m2("🎯", f"RANGING_FP_GATE: {momentum}|{volatility}|{trend} "
                                          f"WR={_rf_wr:.0%} n={_rf_samples:.0f} score={result['score']:.1f} — entro size 0.5x")
                        result['enter'] = True
                        result['size'] = min(result['size'], 0.5)
                    else:
                        if result['score'] > 50 and len(self._phantoms_open) < 5:
                            self._record_phantom(price, f"SCORE_SOTTO_{result['score']:.0f}_vs_{result['soglia']:.0f}",
                                                seed['score'], momentum, volatility, trend)
                        return

            # -- MIDZONE FILTER (regola del trader) ────────────────────────
            # In RANGING centrale (40-60% del range) ZERO TRADE
            # Il drift deve essere vero — non rumore
            if self._regime_current == "RANGING":
                prices_buf = list(self.campo._prices_short)[-20:] if len(self.campo._prices_short)>=20 else []
                if prices_buf:
                    r20 = max(prices_buf) - min(prices_buf)
                    if r20 > 0:
                        range_pos = (price - min(prices_buf)) / r20

                        # Midzone: prezzo nel 40-60% del range → size ridotta, non blocco
                        if 0.40 <= range_pos <= 0.60:
                            # Se OracoloInterno in FUOCO con carica alta → entra con size 0.3x
                            if self._oi_stato == "FUOCO" and self._oi_carica >= 0.65 and getattr(self, "_last_fingerprint_wr", 0) >= 0.55:
                                self._log_m2("⚠️", f"MIDZONE pos={range_pos:.2f} — FUOCO attivo, entro size 0.3x")
                                result['size'] = min(result.get('size', 1.0), 0.3)
                            else:
                                if len(self._phantoms_open) < 5:
                                    self._record_phantom(price, f"MIDZONE_pos{range_pos:.2f}",
                                        seed['score'], momentum, volatility, trend)
                                self._log_m2("🚫", f"MIDZONE BLOCK pos={range_pos:.2f} — no trade in centro range")
                                return
                        # Drift troppo debole — rumore puro
                        # MA: se Oracolo è in FUOCO/CARICA con carica alta → ignora drift debole
                        drift_avg = abs(_drift_real)
                        if drift_avg < 0.0001 and range_pos < 0.80:
                            self._log_m2("🚫", f"DRIFT DEBOLE {drift_avg:.5f} — no trade")
                            return

            # -- HARD GUARD: doppio check anti-bug --------------------------
            # DeepSeek può abbassare la soglia temporaneamente
            _ds_soglia = getattr(self.campo, '_soglia_min_override', None)
            _result_soglia = result['soglia']
            if _ds_soglia is not None and _ds_soglia < _result_soglia:
                self._log_m2("🤖", f"DS soglia override: {_result_soglia:.1f}→{_ds_soglia:.1f}")
                result['soglia'] = _ds_soglia

            if result['score'] < result['soglia']:
                # FUOCO BYPASS anche sul HARD GUARD
                if self._oi_stato == "FUOCO" and self._oi_carica >= 0.65 and getattr(self, "_last_fingerprint_wr", 0) >= 0.55:
                    self._log_m2("🔥", f"HARD GUARD bypassed — FUOCO carica={self._oi_carica:.2f} score={result['score']:.1f}")
                else:
                    self._log_m2("🛑", f"HARD GUARD: score={result['score']:.1f} < soglia={result['soglia']:.1f} - BLOCCATO")
                    return

            # -- LEGGE OPERATIVA: predizione forte blocca SC ─────────────
            # Se la predizione storica dice BLOCCA con confidenza reale
            # SC non può scavalcare — la memoria locale è sovrana
            _pred_veto = self.signal_tracker.predict_from_signals(
                regime=self._regime_current,
                direction=self.campo._direction,
                score=result['score'],
                drift=_drift_real,
                rsi=self.campo._last_rsi,
            )
            if _pred_veto['confidence'] >= 0.3 and _pred_veto['verdict'] == 'BLOCCA':
                if self._oi_stato == "FUOCO" and self._oi_carica >= 0.65 and getattr(self, "_last_fingerprint_wr", 0) >= 0.55:
                    self._log_m2("🔥", f"PRED_VETO bypassed — FUOCO carica={self._oi_carica:.2f}")
                else:
                    self._log_m2("🔮", f"PRED_VETO SC — hit={_pred_veto['hit_rate']:.0%} "
                                       f"n={_pred_veto['n_vicini']} — SC non può scavalcare")
                    if len(self._phantoms_open) < 5:
                        self._record_phantom(price,
                            f"PRED_VETO_SC_hr{_pred_veto['hit_rate']:.0%}",
                            seed['score'], momentum, volatility, trend)
                    return

            # -- SUPERCERVELLO: decisione unificata da tutti gli organi ────
            # Legge simultaneamente tutti i sistemi e decide una volta sola.
            _mat_wr    = self.memoria.get_wr(matrimonio_name) if hasattr(self.memoria,'get_wr') else 0.55
            _mat_trust = self.memoria.get_trust(matrimonio_name) if hasattr(self.memoria,'get_trust') else 0.6
            _ph_prot   = self._phantom_stats.get('total_saved', 0.0)
            _ph_zav    = self._phantom_stats.get('total_missed', 0.0)
            _st_data   = self._get_signal_tracker_context(self._regime_current, result['score'])

            # GUARD ASSOLUTO: se campo.evaluate ha dato veto TOSSICO,
            # il SuperCervello non può bypassarlo con VERITAS_FUOCO
            if result.get('veto') and (
                result['veto'].startswith("TOSSICO") or
                result['veto'].startswith("CM_TOSSICO") or
                result['veto'].startswith("STATIC_TOSSICO")
            ):
                if len(self._phantoms_open) < 5:
                    self._record_phantom(price, result['veto'], result.get('score',0), momentum, volatility, trend)
                return

            _sc_dec = self.supercervello.decide(
                fp_wr=fingerprint_wr, fp_samples=int(self.oracolo._memory.get(
                    f"{momentum}|{volatility}|{trend}|{self.campo._direction}",{}).get('samples',0)),
                st_hit_rate=_st_data.get('hit_rate', 0.5),
                st_n=_st_data.get('n', 0),
                st_pnl=_st_data.get('pnl_sim', 0.0),
                oi_carica=self._oi_carica,
                oi_stato=self._oi_stato,
                score=result['score'], soglia=result['soglia'],
                matrimonio_wr=_mat_wr, matrimonio_trust=_mat_trust,
                ph_protezione=_ph_prot, ph_zavorra=_ph_zav,
                regime=self._regime_current,
                midzone=False,  # midzone già gestito sopra
                loss_streak=self._m2_loss_streak,
            )

            # Applica azioni capsule auto-correttive prima della decisione SC
            _m2_caps = self.capsule_runtime.valuta({
                'regime': self._regime_current,
                'direction': self.campo._direction,
                'oi_carica': self._oi_carica,
                'oi_stato': self._oi_stato,
                'drift': _drift_real,
                'loss_streak': self._m2_loss_streak,
            })
            if _m2_caps.get('ripristina_pesi_sc'):
                pesi = _m2_caps['ripristina_pesi_sc']
                if pesi:
                    self.supercervello._pesi.update(pesi)
                    self._log_m2("🔧", f"[CAPSULA] Pesi SC ripristinati")
            if _m2_caps.get('sblocca_short_ranging'):
                if _drift_real < -0.02:
                    self.campo._direction = "SHORT"
                    self._log_m2("🔧", f"[CAPSULA] SHORT sbloccato in RANGING")
            if _m2_caps.get('cap2_soglia') is not None:
                self._cap2_soglia_override = _m2_caps['cap2_soglia']
                self._log_m2("🔧", f"[CAPSULA] Capsule2 soglia auto-calibrata: {_m2_caps['cap2_soglia']:.2f}")

            # -- DEEPSEEK OVERRIDE: se DS ha comandato, ignora SC ──────────
            _ds_blocca_sc = getattr(self, '_ds_blocca_sc', False)
            _ds_forza_entry = getattr(self, '_ds_forza_entry', False)

            # -- FUOCO PERMANENTE: quando OracoloInterno è in FUOCO con carica >= 0.65
            # il SC non può bloccare — il Veritas ha dimostrato che SC sbaglia sistematicamente
            # ECCEZIONE: in EXPLOSIVE il FUOCO non è affidabile — troppa volatilità
            _fuoco_bypass = (self._oi_stato == "FUOCO" and 
                             self._oi_carica >= 0.65 and
                             self._regime_current != "EXPLOSIVE")

            # Applica decisione supercervello
            if _sc_dec['azione'] == 'BLOCCA':
                if _ds_blocca_sc or _ds_forza_entry or _fuoco_bypass:
                    if _fuoco_bypass:
                        self._log_m2("🔥", f"FUOCO BYPASS SC — OI={self._oi_stato} carica={self._oi_carica:.2f} — Oracolo decide")
                    else:
                        self._log_m2("🤖", f"DS OVERRIDE SC BLOCCA → entro lo stesso (DS comando attivo)")
                    self._ds_forza_entry = False
                else:
                    self._log_m2("🧠", f"SC BLOCCA — {_sc_dec['motivo']}")
                    self.veritas.registra(price, self._oi_stato, self._oi_carica,
                        "BLOCCA", _sc_dec['confidenza'], self._regime_current, time.time())
                    if len(self._phantoms_open) < 5:
                        self._record_phantom(price, f"SC_BLOCCA_c{_sc_dec['confidenza']:.2f}",
                            seed['score'], momentum, volatility, trend)
                    return

            # Aggiusta size e soglia in base alla confidenza
            if _sc_dec['azione'] == 'ENTRA':
                result['size'] = round(result['size'] * _sc_dec['size_mult'], 3)
                if _sc_dec['soglia_adj'] != 0:
                    result['soglia'] = max(self.campo.SOGLIA_MIN,
                                          result['soglia'] + _sc_dec['soglia_adj'])
                self._log_m2("🧠", f"SC ENTRA — {_sc_dec['motivo']} size×{_sc_dec['size_mult']}")
                if result['size'] < 0.05 and self._oi_stato == "FUOCO" and self._oi_carica >= 0.65:
                    result['size'] = 0.3
                    self._log_m2("🔥", f"SIZE FORCED 0.3 — FUOCO carica={self._oi_carica:.2f}")
                self._last_sc_dec = _sc_dec
                self.veritas.registra(price, self._oi_stato, self._oi_carica,
                    "ENTRA", _sc_dec['confidenza'], self._regime_current, time.time())

            # -- LEGGE: predizione forte = entry pilota obbligatoria ──────
            # Se predizione è fortemente aderente al mercato e SC non ha detto BLOCCA
            # garantisci almeno size minima — non zero. La predizione diventa esecuzione.
            if (_pred_veto.get('confidence', 0) >= 0.4
                    and _pred_veto.get('verdict') == 'ENTRA'
                    and _pred_veto.get('hit_rate', 0) >= 0.60
                    and _sc_dec['azione'] != 'BLOCCA'):
                result['size'] = max(result['size'], 0.5)  # pilota minimo garantito
                self._log_m2("🔮", f"PRED_PILOT — hit={_pred_veto['hit_rate']:.0%} "
                                   f"n={_pred_veto['n_vicini']} → size minima garantita {result['size']:.2f}x")

            



            # ===============================================================
            # ENERGY FILTER - la volpe caccia solo prede che valgono
            #
            # Non basta passare la soglia. Il trade deve avere ENERGIA
            # sufficiente a produrre un delta che copra le fee.
            #
            # 1. Score >= MIN_SCORE_ECONOMICO (58)
            #    Solo trade con eccedenza alta producono delta > $30
            #
            # 2. SEED_TREND crescente (3 su 5 ultimi seed crescenti)
            #    L'impulso deve essere in NASCITA, non un picco isolato
            #
            # Calibrato su 500+ trade reali:
            #   score < 58: delta medio $15-25, pnl NEGATIVO dopo fee
            #   score >= 58: delta medio $60+, pnl POSITIVO
            # ===============================================================
            
            # ── ENERGY FILTER ECONOMICO SOVRANO ─────────────────────────
            # La verità economica locale comanda — non la soglia del campo.
            # Legge signal_tracker._stats per la chiave {regime}|{direction}|{band}
            # e decide BLOCK / PILOT / FULL dai dati reali accumulati.
            #
            # A. n>=100 e avg_pnl_sim<=-0.05        → ECON_BLOCK
            # B. avg_pnl_sim<0 o n<20 o pos<0.55    → ECON_PILOT (size cappata)
            # C. avg_pnl_sim>=+0.05 e pos>=0.55 e n>=20 → ECON_OK (FULL)
            _score = result['score']
            if _score >= 75:   _band = "FORTE_75+"
            elif _score >= 65: _band = "BUONO_65-75"
            elif _score >= 58: _band = "BASE_58-65"
            else:              _band = "DEBOLE_<58"
            _econ_key = f"{self._regime_current}|{self.campo._direction}|{_band}"
            _st = getattr(self.signal_tracker, '_stats', {}).get(_econ_key, {})
            _n         = _st.get('n', 0)
            _pnls      = _st.get('pnl_sim', [])
            _hits      = _st.get('hit_60', [])
            _avg_pnl   = sum(_pnls) / len(_pnls) if _pnls else None
            _pnl_pos   = sum(1 for p in _pnls if p > 0) / len(_pnls) if _pnls else None

            if _avg_pnl is not None and _n >= 100 and _avg_pnl <= -0.05:
                    # Bypass ECON_BLOCK se OracoloInterno in FUOCO con carica alta
                    if self._oi_stato == "FUOCO" and self._oi_carica >= 0.65 and getattr(self, "_last_fingerprint_wr", 0) >= 0.55:
                        self._log_m2("🔥", f"ECON_BLOCK bypassed — FUOCO carica={self._oi_carica:.2f}")
                    else:
                        self._log_m2("💸", f"ECON_BLOCK {_econ_key} avg_pnl={_avg_pnl:.3f} n={_n}")
                        if len(self._phantoms_open) < 5:
                            self._record_phantom(price, f"ECON_BLOCK_{_econ_key}",
                                seed['score'], momentum, volatility, trend)
                        return

            elif (_avg_pnl is None or _avg_pnl < 0 or _n < 20 or
                  (_pnl_pos is not None and _pnl_pos < 0.55)):
                # B: dati insufficienti o edge debole — PILOT (size cappata)
                if self._oi_stato == "FUOCO" and self._oi_carica >= 0.65 and getattr(self, "_last_fingerprint_wr", 0) >= 0.55:
                    self._log_m2("🔥", f"ECON_PILOT bypassed — FUOCO carica={self._oi_carica:.2f}")
                elif _band == "DEBOLE_<58":
                    result['size'] = min(result['size'], 0.10)
                    result['soglia'] = max(result['soglia'], 58)
                elif _band == "BASE_58-65":
                    result['size'] = min(result['size'], 0.15)
                if not (self._oi_stato == "FUOCO" and self._oi_carica >= 0.65):
                    _pilot_reason = (f"n={_n}" if _n < 20 else
                                     f"avg_pnl={_avg_pnl:.3f}" if _avg_pnl is not None and _avg_pnl < 0 else
                                     f"pos={_pnl_pos:.0%}" if _pnl_pos is not None else "no_data")
                    self._log_m2("🔬", f"ECON_PILOT {_econ_key} {_pilot_reason} size→{result['size']:.2f}")
            else:
                # C: evidenza positiva — FULL
                self._log_m2("✅", f"ECON_OK {_econ_key} avg_pnl={_avg_pnl:.3f} pos={_pnl_pos:.0%} n={_n}")

            # ===============================================================
            # REGIME-AWARE BEHAVIOR - il laterale è un altro mestiere
            #
            # RANGING: no-trade zone al centro, min hold lungo, più selettivo
            # TRENDING: fluido, comportamento quasi invariato
            # EXPLOSIVE: lascia correre, min hold corto
            # ===============================================================
            
            if self._regime_current == "RANGING":
                # -- NO-TRADE ZONE: non tradare al centro del range ----
                regime_prices = list(self.regime_detector.prices)
                if len(regime_prices) >= 200:
                    recent = regime_prices[-200:]
                    range_high = max(recent)
                    range_low = min(recent)
                    range_size = range_high - range_low
                    
                    if range_size > 0:
                        position_in_range = (price - range_low) / range_size
                        if 0.40 <= position_in_range <= 0.60:
                            if self._oi_stato == "FUOCO" and self._oi_carica >= 0.65 and getattr(self, "_last_fingerprint_wr", 0) >= 0.55:
                                self._log_m2("⚠️", f"RANGE_MIDZONE2 pos={position_in_range:.0%} — FUOCO, size 0.3x")
                                result['size'] = min(result.get('size', 1.0), 0.3)
                            else:
                                if len(self._phantoms_open) < 5:
                                    self._record_phantom(price,
                                        f"RANGE_MIDZONE_{position_in_range:.0%}",
                                        seed['score'], momentum, volatility, trend)
                                return

            # ===============================================================
            # ORACOLO 2.0 - CAPSULE STATICHE + CONTEXT-MATCHING
            # Il cervello della volpe decide se QUESTA situazione vale
            # ===============================================================
            
            # Calcola contesto per Oracolo
            _oc_rsi = getattr(self.campo, '_last_rsi', 50)
            _oc_drift = 0.0
            _oc_rpos = 0.5
            if len(self.campo._prices_long) >= 100:
                _p = list(self.campo._prices_long)
                _oc_drift = (sum(_p[-50:])/50 - sum(_p[:50])/50) / (sum(_p[:50])/50) * 100
            if len(self.campo._prices_long) >= 200:
                _r = list(self.campo._prices_long)[-200:]
                _rh, _rl = max(_r), min(_r)
                if _rh > _rl:
                    _oc_rpos = (price - _rl) / (_rh - _rl)
            
            # Capsule OC1-OC5
            oc_block, oc_reason = self.oracolo.check_capsules(
                self._regime_current, self.campo._direction, _oc_rsi,
                _oc_drift, _oc_rpos, momentum, self._m2_loss_streak)
            if oc_block:
                if self._oi_stato == "FUOCO" and self._oi_carica >= 0.65 and getattr(self, "_last_fingerprint_wr", 0) >= 0.55:
                    self._log_m2("🔥", f"OC_BLOCK bypassed — FUOCO carica={self._oi_carica:.2f}")
                else:
                    if len(self._phantoms_open) < 5:
                        self._record_phantom(price, oc_reason, seed['score'], momentum, volatility, trend)
                    return

            # Context-matching: cerca trade simili passati
            ctx = self.oracolo.context_match(
                self._regime_current, momentum, volatility, trend,
                self.campo._direction, _oc_rsi, _oc_drift, _oc_rpos)
            if ctx['verdict'] == 'BLOCCA' and ctx['confidence'] > 0.4:
                if self._oi_stato == "FUOCO" and self._oi_carica >= 0.65 and getattr(self, "_last_fingerprint_wr", 0) >= 0.55:
                    self._log_m2("🔥", f"CTX_MATCH bypassed — FUOCO carica={self._oi_carica:.2f}")
                else:
                    if len(self._phantoms_open) < 5:
                        self._record_phantom(price,
                            f"CTX_MATCH_BLOCK_pnl{ctx['pnl_predicted']:+.1f}_w{ctx['wins']}/5",
                            seed['score'], momentum, volatility, trend)
                    return

            self._log_m2("🎯", f"ENTRY {self.campo._direction} {matrimonio_name} | score={result['score']:.1f} "
                              f"soglia={result['soglia']:.1f} size={result['size']:.2f}x "
                              f"| {result['breakdown']} @ ${price:.1f}")

            self._shadow = {
                "price_entry":   price,
                "matrimonio":    matrimonio_name,
                "duration_avg":  matrimonio["duration_avg"],
                "size":          result['size'],
                "score":         result['score'],
                "soglia":        result['soglia'],
                "pb_signals":    result.get('pb_signals', 0),
                "direction":     self.campo._direction,
                "regime_entry":  self._regime_current,
            }
            self._shadow_entry_time        = time.time()
            self._shadow_entry_momentum    = momentum
            self._shadow_entry_volatility  = volatility
            self._shadow_entry_trend       = trend
            self._shadow_entry_fingerprint = fingerprint_wr
            self._shadow_max_price         = price
            self._shadow_min_price         = price
            self._shadow_matrimonio        = matrimonio_name
            self._m2_trades += 1

            # -- TELEMETRY: registra entry ---------------------------------
            self.telemetry.log_trade_entry(
                trade_direction=self.campo._direction,
                score=result['score'], soglia=result['soglia'],
                matrimonio=matrimonio_name,
                regime=self._regime_current, direction=self.campo._direction,
                open_position=True
            )

            # -- SCRIVI ENTRY NEL DATABASE ---------------------------------
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("""
                    INSERT INTO trades (event_type, asset, price, size, pnl, direction, reason, data_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, ("M2_ENTRY", SYMBOL, price, result['size'], 0.0,
                      f"{self.campo._direction}_SHADOW", f"score={result['score']:.1f} soglia={result['soglia']:.1f}",
                      json.dumps({
                          "motore": "M2", "matrimonio": matrimonio_name,
                          "score": result['score'], "soglia": result['soglia'],
                          "momentum": momentum, "volatility": volatility,
                          "trend": trend, "regime": self._regime_current,
                          "breakdown": result['breakdown'],
                          "direction": self.campo._direction,
                      })))
                conn.commit()
                conn.close()
            except Exception as e:
                log.error(f"[M2_DB] Entry save: {e}")

        except Exception as e:
            import traceback
            self._log_m2("💥", f"ERRORE shadow_entry: {e}")
            log.error(f"[M2_ENTRY_ERROR] {e}\n{traceback.format_exc()}")

    def _evaluate_shadow_exit(self, price, momentum, volatility, trend):
        """Stessa logica di uscita del Motore 1 applicata al shadow trade."""
        try:
            if not self._shadow:
                return
            if price > self._shadow_max_price:
                self._shadow_max_price = price
            if price < self._shadow_min_price:
                self._shadow_min_price = price

            duration     = time.time() - self._shadow_entry_time
            duration_avg = self._shadow["duration_avg"]
            
            # CRITICO: direzione al momento dell'ENTRY, non quella attuale
            entry_direction = self._shadow.get("direction", "LONG")

            # -- HARD STOP LOSS 2% SUL PNL REALE --------------------------
            # Stop sul PnL della posizione, non sul prezzo.
            # Formula: delta% × esposizione
            # 2% di $5000 esposizione = $100 max loss
            # Su SOL $130: $100 / 38.46 = $2.60 movimento — ragionevole
            exposure_sl = self.TRADE_SIZE_USD * self.LEVERAGE
            btc_qty_sl = exposure_sl / self._shadow["price_entry"]
            if entry_direction == "SHORT":
                current_pnl_real = (self._shadow["price_entry"] - price) * btc_qty_sl
            else:
                current_pnl_real = (price - self._shadow["price_entry"]) * btc_qty_sl

            HARD_STOP_USD = exposure_sl * 0.02  # 2% dell'esposizione = $100 su $5000
            if current_pnl_real < -HARD_STOP_USD:
                self._close_shadow_trade(price, f"HARD_STOP_${abs(current_pnl_real):.1f}_max${HARD_STOP_USD:.0f}")
                return

            # -- MINIMUM HOLD TIME ---------------------------------------------
            # FIX: MIN_HOLD_SECONDS era dichiarato ma mai applicato.
            # Nessun divorzio nei primi 10 secondi — il trade deve respirare.
            MIN_HOLD_SECONDS = 10

            # FIX: drawdown_pct calcolato sempre — serve anche per TIMEOUT_DD
            if entry_direction == "SHORT":
                drawdown_pct = ((price - self._shadow_min_price) / self._shadow["price_entry"]) * 100
            else:
                drawdown_pct = ((self._shadow_max_price - price) / self._shadow["price_entry"]) * 100

            if duration >= MIN_HOLD_SECONDS:
                # -- 4 DIVORCE TRIGGERS (attivi solo dopo MIN_HOLD) ---------------
                triggers = []
                if self._shadow_entry_volatility == "BASSA" and volatility == "ALTA" and current_pnl_real < 0:
                    triggers.append("T1_VOL")
                # T2: trend inverte CONTRO la nostra direzione
                if entry_direction == "LONG" and self._shadow_entry_trend == "UP" and trend == "DOWN":
                    triggers.append("T2_TREND")
                elif entry_direction == "SHORT" and self._shadow_entry_trend == "DOWN" and trend == "UP":
                    triggers.append("T2_TREND")
                # T3: drawdown dal migliore raggiunto
                if drawdown_pct > DIVORCE_DRAWDOWN_PCT:
                    triggers.append("T3_DD")
                # T4: FIX — scatta solo se in perdita, non se stai guadagnando
                current_fp = self.oracolo.get_wr(momentum, volatility, trend, entry_direction)
                fp_div = abs(current_fp - self._shadow_entry_fingerprint) / max(self._shadow_entry_fingerprint, 0.001)
                if fp_div > DIVORCE_FP_DIVERGE_PCT and current_pnl_real < 0:
                    triggers.append("T4_FP")
                if len(triggers) >= DIVORCE_MIN_TRIGGERS:
                    self._close_shadow_trade(price, f"DIVORZIO|{'|'.join(triggers)}")
                    return

            # ===============================================================
            # EXIT INTELLIGENTE - CAMPO GRAVITAZIONALE DI USCITA
            # Stessa filosofia dell'entry: legge l'energia, non l'orologio.
            #
            # L'impulso nasce (entry), vive (hold), muore (exit).
            # L'exit misura l'energia RESIDUA dell'impulso:
            #   - Momentum ancora vivo? → resta
            #   - Prezzo ancora nella direzione? → resta  
            #   - Decelerazione forte? → prepara uscita
            #   - Inversione confermata? → esci
            #
            # Score di uscita 0-100:
            #   0  = impulso morto, esci subito
            #   50 = neutro, monitora
            #   100 = impulso ancora forte, resta dentro
            # ===============================================================
            
            if entry_direction == "LONG":
                current_pnl = price - self._shadow["price_entry"]
                max_profit = self._shadow_max_price - self._shadow["price_entry"]
                retreat = self._shadow_max_price - price
            else:
                current_pnl = self._shadow["price_entry"] - price
                max_profit = self._shadow["price_entry"] - self._shadow_min_price
                retreat = price - self._shadow_min_price

            # -- COMPONENTE 1: MOMENTUM (peso 30) ---------------------
            # FORTE=30, MEDIO=20, DEBOLE=5
            # In direzione giusta = punteggio pieno
            if entry_direction == "LONG":
                mom_score = {'FORTE': 30, 'MEDIO': 20, 'DEBOLE': 5}.get(momentum, 15)
            else:
                mom_score = {'DEBOLE': 30, 'MEDIO': 20, 'FORTE': 5}.get(momentum, 15)
            
            # -- COMPONENTE 2: TREND (peso 20) -------------------------
            if entry_direction == "LONG":
                trend_score = {'UP': 20, 'SIDEWAYS': 10, 'DOWN': 0}.get(trend, 10)
            else:
                trend_score = {'DOWN': 20, 'SIDEWAYS': 10, 'UP': 0}.get(trend, 10)
            
            # -- COMPONENTE 3: DECELERAZIONE (peso 25) -----------------
            # Derivata seconda: l'impulso sta frenando?
            decel = self.decelero.analyze()
            decel_score_val = decel.get('decel_score', 0)
            # Bassa decelerazione = alto punteggio (resta)
            decel_comp = int((1.0 - decel_score_val) * 25)
            
            # -- COMPONENTE 4: PROFITTO PROTETTO (peso 25) -------------
            # La tolleranza al retreat non è fissa. Dipende dalla volatilità
            # del fingerprint: pattern volatile → retreat normale è alto.
            # L'Oracolo conosce la volatilità media dei WIN su questo pattern.
            #
            # Se non ci sono dati → usa 50% come neutro (nessun numero fisso).
            if max_profit > 0:
                retreat_pct = retreat / max_profit
                # Tolleranza adattiva: deriva dal PnL medio dei WIN su questo pattern.
                # PnL win alto → il trade ha ampio respiro → tolleranza alta.
                # PnL win basso → trade stretto → tolleranza bassa.
                pnl_win_avg = abs(self.oracolo.get_pnl_avg(
                    self._shadow_entry_momentum or momentum,
                    self._shadow_entry_volatility or volatility,
                    self._shadow_entry_trend or trend,
                    direction=entry_direction
                )) or 5.0
                # Tolleranza: da 40% (trade stretto) a 70% (trade ampio)
                # Calibrata sui dati reali, non su un numero fisso
                tolleranza = min(0.70, max(0.40, 0.40 + (pnl_win_avg / 50.0) * 0.30))
                penalized = max(0.0, (retreat_pct - tolleranza) / (1.0 - tolleranza))
                profit_comp = int((1.0 - min(1.0, penalized)) * 25)
            elif current_pnl < 0:
                profit_comp = 5
            else:
                profit_comp = 15
            
            # -- SCORE TOTALE EXIT -------------------------------------
            exit_energy = mom_score + trend_score + decel_comp + profit_comp
            
            # -- EXIT INTELLIGENTE: la soglia nasce dai dati, non da manopole --
            #
            # Il sistema misura tre cose reali:
            #   1. Quanto durano i WIN su questo pattern (Oracolo duration memory)
            #   2. Quanto spesso esce troppo presto (post-trade tracker)
            #   3. Come si muove il prezzo dopo l'uscita (delta post-trade)
            #
            # Da questi tre dati emerge la soglia giusta — non da un numero fisso.

            fp_entry = self.oracolo._fp(
                self._shadow_entry_momentum or momentum,
                self._shadow_entry_volatility or volatility,
                self._shadow_entry_trend or trend,
                entry_direction
            )

            # MIN_HOLD: 70% della durata media dei WIN su questo fingerprint.
            # Se non ci sono dati sufficienti → usa la durata media del regime corrente
            # dai trade reali in memoria. Zero default fisso.
            MIN_HOLD = self.oracolo.get_dynamic_min_hold(
                self._shadow_entry_momentum or momentum,
                self._shadow_entry_volatility or volatility,
                self._shadow_entry_trend or trend,
                direction=entry_direction,
                regime=self._regime_current
            )

            if duration < MIN_HOLD:
                return  # il tempo minimo non è ancora scaduto

            # Soglia base: deriva dal rapporto tra durata corrente e durata media WIN.
            # Se duriamo già il 120% della durata media WIN → soglia sale (chiudi presto).
            # Se duriamo il 50% → soglia bassa (lascia correre ancora).
            avg_win_dur = self.oracolo.get_avg_duration(
                self._shadow_entry_momentum or momentum,
                self._shadow_entry_volatility or volatility,
                self._shadow_entry_trend or trend,
                direction=entry_direction, is_win=True
            ) or 60.0

            # Quanto siamo nella vita del trade rispetto alla durata media WIN
            time_ratio = duration / avg_win_dur  # 0.5 = a metà vita, 2.0 = doppio del normale

            # Soglia che sale con il tempo proporzionalmente alla vita del trade
            # Quando siamo a metà vita (ratio=0.5): soglia 32
            # Quando siamo alla fine normale (ratio=1.0): soglia 45
            # Quando siamo oltre (ratio=2.0): soglia 58
            exit_soglia_base = int(25 + time_ratio * 30)
            exit_soglia_base = max(28, min(65, exit_soglia_base))

            # EXIT_TOO_EARLY FEEDBACK: il post-trade dice quanto spesso usciamo presto.
            # Rate alto → il sistema abbassa la soglia → più difficile uscire → resta più a lungo.
            # Rate basso → soglia normale → esce quando l'energia cala.
            # Questo è un ciclo chiuso: il sistema si autocorregge sui propri errori.
            too_early_rate = self.oracolo.get_exit_too_early_rate(fp_entry)

            # Correzione proporzionale: da 0 (rate=50%) a -15pt (rate=100%)
            if too_early_rate > 0.5:
                correzione = int((too_early_rate - 0.5) * 30)  # 0→15 punti di abbassamento
                exit_soglia = max(20, exit_soglia_base - correzione)
                if correzione >= 5:
                    self._log_m2("⏳", f"EXIT_FEEDBACK: early={too_early_rate:.0%} "
                                      f"ratio={time_ratio:.1f} soglia={exit_soglia} "
                                      f"(senza feedback sarebbe {exit_soglia_base})")
            else:
                exit_soglia = exit_soglia_base
            
            # -- DECISIONE ---------------------------------------------
            if exit_energy < exit_soglia:
                if current_pnl > 0:
                    self._close_shadow_trade(price, f"EXIT_E{exit_energy}_S{exit_soglia}_WIN_{current_pnl:+.0f}")
                else:
                    self._close_shadow_trade(price, f"EXIT_E{exit_energy}_S{exit_soglia}")
                return

            # -- TIMEOUT SAFETY - solo se l'exit intelligente non chiude -----
            # Niente TIMEOUT_3X - l'exit intelligente decide.
            # Solo TIMEOUT_DD: se in drawdown > 1% dopo duration_avg → esci
            if duration > duration_avg * 5 and drawdown_pct > 1.0:
                self._close_shadow_trade(price, "TIMEOUT_DD")
                return
            # TIMEOUT ASSOLUTO: max 3 minuti per trade
            if duration > 180:
                self._close_shadow_trade(price, "TIMEOUT_MAX")
                return

        except Exception as e:
            import traceback
            self._log_m2("💥", f"ERRORE shadow_exit: {e}")
            log.error(f"[M2_EXIT_ERROR] {e}\n{traceback.format_exc()}")

    def _close_shadow_trade(self, price, reason):
        """Chiude il shadow trade e registra stats M2.
        CRITICO: insegna all'Oracolo e persiste su DB - altrimenti il sistema non impara MAI.
        
        NOTA FEE: Il PnL paper NON include fee Binance.
        In live con BNB: 0.075% per lato + ~0.01% slippage = 0.17% round trip.
        Su BTC a $70k = ~$119 per trade su 1 BTC.
        Lo scalping a 10-15s con PnL $5-17 NON è profittevole in spot.
        Serve: futures (fee 0.07% RT) con leva, oppure trade più lunghi con PnL > $150.
        """
        try:
            if not self._shadow:
                return
            # PnL REALE FUTURES = delta_prezzo × quantita BTC nella posizione
            # CRITICO: usa la direzione al momento dell'ENTRY, non quella attuale
            # Se il campo ha flippato durante il trade, la direzione attuale è sbagliata
            entry_direction = self._shadow.get("direction", "LONG")
            delta_price = (price - self._shadow["price_entry"]) if entry_direction == "LONG" \
                  else (self._shadow["price_entry"] - price)
            exposure_usd = self.TRADE_SIZE_USD * self.LEVERAGE
            btc_qty = exposure_usd / self._shadow["price_entry"]
            pnl_gross = delta_price * btc_qty
            
            # FEE FUTURES: 0.02% maker × 2 (andata + ritorno) sulla esposizione
            # $5000 × 0.02% × 2 = $2.00 per trade
            total_fees = exposure_usd * self.FEE_PCT * 2
            
            pnl = pnl_gross - total_fees
            is_win = pnl > 0

            # -- TELEMETRY: registra trade ------------------------------------
            trade_duration = time.time() - self._shadow_entry_time if self._shadow_entry_time else 0
            ctx = self._tele_ctx()
            self.telemetry.log_trade_close(
                trade_direction=self._shadow.get("direction", "LONG"),
                pnl=pnl, is_win=is_win, exit_reason=reason, duration=trade_duration,
                **{k: ctx[k] for k in ('regime','direction','open_position',
                   'active_threshold','drift','macd','trend','volatility')}
            )

            # -- STATE ENGINE: aggiorna stato dopo ogni trade -------------
            self._state_engine_update(pnl, is_win, trade_duration)

            self.campo.record_result(is_win, exit_reason=reason, 
                                     pb_signals=self._shadow.get("pb_signals", 0),
                                     pnl=pnl)

            # -- INSEGNA ALL'ORACOLO 2.0 - il cervello impara TUTTO ---------
            if self._shadow_entry_momentum and self._shadow_entry_volatility and self._shadow_entry_trend:
                # Calcola range_position e drift per contesto
                _rp = 0.5
                _dr = 0.0
                if len(self.campo._prices_long) >= 200:
                    _recent = list(self.campo._prices_long)[-200:]
                    _rh, _rl = max(_recent), min(_recent)
                    if _rh > _rl:
                        _rp = (price - _rl) / (_rh - _rl)
                if len(self.campo._prices_long) >= 100:
                    _p = list(self.campo._prices_long)
                    _dr = (sum(_p[-50:])/50 - sum(_p[:50])/50) / (sum(_p[:50])/50) * 100

                self.oracolo.record(
                    self._shadow_entry_momentum,
                    self._shadow_entry_volatility,
                    self._shadow_entry_trend,
                    is_win,
                    direction=entry_direction,
                    pnl=pnl,
                    duration=trade_duration,
                    rsi=getattr(self.campo, '_last_rsi', 50),
                    drift=_dr,
                    range_position=_rp,
                    regime=self._regime_current,
                    hour=datetime.utcnow().hour,
                )
                
                # Avvia post-trade tracker
                fp = self.oracolo._fp(self._shadow_entry_momentum,
                                       self._shadow_entry_volatility,
                                       self._shadow_entry_trend,
                                       entry_direction)
                self.oracolo.start_post_trade(fp, price, entry_direction)

            # -- AGGIORNA MEMORIA MATRIMONI - anche M2 conta ------------------
            if self._shadow_matrimonio:
                matrimonio = MatrimonioIntelligente.get_by_name(self._shadow_matrimonio)
                wr_expected = matrimonio.get("wr", 0.50)
                self.memoria.record_trade(self._shadow_matrimonio, is_win, wr_expected)

            # -- CALIBRATORE - M2 insegna anche a lui -------------------------
            self.calibratore.registra_osservazione(
                seed_score=self._shadow.get("score", 0) / 100.0,
                fingerprint_wr=self._shadow_entry_fingerprint or 0.72,
                is_win=is_win,
                divorce_drawdown_usato=((self._shadow_max_price - price) / self._shadow["price_entry"] * 100)
                                       if self._shadow_max_price and self._shadow.get("price_entry") else 0.0
            )

            # -- INTELLIGENZA AUTONOMA - M2 registra con contesto completo ----
            # Calcola drift corrente per le capsule L2_DRIFT
            _ia_drift = 0.0
            if len(self.campo._prices_long) >= 100:
                _p = list(self.campo._prices_long)
                _ia_drift = (sum(_p[-50:])/50 - sum(_p[:50])/50) / (sum(_p[:50])/50) * 100

            self.realtime_engine.registra_trade({
                'matrimonio': self._shadow_matrimonio,
                'pnl':        pnl,
                'is_win':     is_win,
                'regime':     self._regime_current,
                'volatility': self._shadow_entry_volatility or 'MEDIA',
                'trend':      self._shadow_entry_trend or 'SIDEWAYS',
                'direction':  entry_direction,
                'drift':      round(_ia_drift, 4),
                'score':      self._shadow.get('score', 0) if self._shadow else 0,
                'exit_reason': reason,
            })

            # -- LOG ANALYZER - stats per matrimonio includono M2 -------------
            self.log_analyzer.registra({
                'matrimonio': self._shadow_matrimonio, 'pnl': pnl, 'is_win': is_win
            })

            if is_win:
                self._m2_wins  += 1
            else:
                self._m2_losses += 1
            self._m2_pnl += pnl

            # FIX: aggiorna _m2_recent_trades — usato dal gate CESPUGLIO_AVVELENATO
            # Senza questo il gate non ha mai dati e non funziona
            self._m2_recent_trades.append({
                'ts':       time.time(),
                'pnl':      pnl,
                'is_win':   is_win,
                'duration': trade_duration,
                'regime':   self._regime_current,
                'soglia':   self._shadow.get('soglia', 60) if self._shadow else 60,
            })

            m2_tot = self._m2_wins + self._m2_losses
            m2_wr  = (self._m2_wins / m2_tot * 100) if m2_tot > 0 else 0

            self._log_m2(
                "🟢" if is_win else "🔴",
                f"EXIT {self._shadow.get('direction', 'LONG')} {self._shadow_matrimonio} {'WIN' if is_win else 'LOSS'} "
                f"PnL=${pnl:+.4f} WR={m2_wr:.0f}% score={self._shadow['score']:.1f} "
                f"soglia={self._shadow['soglia']:.1f} [{reason}]"
            )

            # -- SCRIVI NEL DATABASE - sopravvive ai restart -------------------
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("""
                    INSERT INTO trades (event_type, asset, price, size, pnl, direction, reason, data_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, ("M2_EXIT", SYMBOL, price, self._shadow.get("size", 0.5), pnl,
                      f"{self._shadow.get('direction', 'LONG')}_SHADOW", reason,
                      json.dumps({
                          "motore": "M2",
                          "matrimonio": self._shadow_matrimonio,
                          "score": self._shadow.get("score", 0),
                          "soglia": self._shadow.get("soglia", 0),
                          "entry_price": self._shadow.get("price_entry", 0),
                          "momentum": self._shadow_entry_momentum,
                          "volatility": self._shadow_entry_volatility,
                          "trend": self._shadow_entry_trend,
                          "is_win": is_win,
                          "direction": self._shadow.get("direction", "LONG"),
                      })))
                conn.commit()
                conn.close()
            except Exception as e:
                log.error(f"[M2_DB] Errore salvataggio trade: {e}")

            # -- PERSISTI IL CERVELLO - Oracolo, Memoria, Calibratore ----------
            self._persist.save_brain(self.oracolo, self.memoria, self.calibratore)

            # -- PERSISTI STATS M2 - sopravvivono ai restart ------------------
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("INSERT OR REPLACE INTO bot_state VALUES ('m2_wins', ?)", (str(self._m2_wins),))
                conn.execute("INSERT OR REPLACE INTO bot_state VALUES ('m2_losses', ?)", (str(self._m2_losses),))
                conn.execute("INSERT OR REPLACE INTO bot_state VALUES ('m2_pnl', ?)", (str(self._m2_pnl),))
                conn.execute("INSERT OR REPLACE INTO bot_state VALUES ('m2_trades', ?)", (str(self._m2_trades),))
                conn.commit()
                conn.close()
            except Exception as e:
                log.error(f"[M2_PERSIST] {e}")

            # -- LOG NARRATIVO -------------------------------------------------
            self.ai_explainer.log_decision("M2_EXIT",
                f"M2 shadow {self._shadow_matrimonio} | PnL=${pnl:+.4f} | {reason}",
                {'pnl': pnl, 'is_win': is_win, 'reason': reason,
                 'score': self._shadow.get('score', 0), 'soglia': self._shadow.get('soglia', 0)})

        except Exception as e:
            import traceback
            self._log_m2("💥", f"ERRORE close_shadow: {e}")
            log.error(f"[M2_CLOSE_ERROR] {e}\n{traceback.format_exc()}")
        finally:
            # Reset shadow SEMPRE - anche se c'è un errore, non lasciare trade fantasma
            self._shadow                   = None
            self._shadow_entry_time        = None
            self._shadow_entry_momentum    = None
            self._shadow_entry_volatility  = None
            self._shadow_entry_trend       = None
            self._shadow_entry_fingerprint = None
            self._shadow_max_price         = None
            self._shadow_min_price         = None
            self._shadow_matrimonio        = None

    def _get_ia_soglia_boost(self, momentum: str, volatility: str, trend: str) -> float:
        """
        Legge le capsule L3 attive di tipo boost_soglia e ritorna il delta totale.
        Il campo.evaluate lo applica sopra la soglia calcolata (con floor=48 invariato).
        """
        try:
            caps = self.capsule_runtime.capsules
            ora  = time.time()
            boost = 0.0
            for c in caps:
                if not c.get('enabled'):
                    continue
                if c.get('scade_ts') and c['scade_ts'] < ora:
                    continue
                azione = c.get('azione', {})
                if azione.get('type') != 'boost_soglia':
                    continue
                # Verifica trigger (può essere vuoto = sempre attivo)
                triggers = c.get('trigger', [])
                ctx = {'momentum': momentum, 'volatility': volatility, 'trend': trend}
                if triggers and not all(self.capsule_runtime._check_trigger(t, ctx) for t in triggers):
                    continue
                boost += azione.get('params', {}).get('delta', 0.0)
            return boost
        except Exception:
            return 0.0

    def _m2_loss_consecutivi(self) -> int:
        """Loss consecutivi del Motore 2."""
        count = 0
        for r in reversed(list(self.campo._recent_results)):
            if not r:
                count += 1
            else:
                break
        return count

    # ========================================================================
    # PHANTOM TRACKER - "SE AVESSI FATTO"
    # Traccia i trade bloccati e calcola cosa sarebbe successo.
    # Zavorra o protezione? I numeri rispondono.
    # ========================================================================

    def _record_phantom(self, price, block_reason, seed_score, momentum, volatility, trend):
        """Registra un trade fantasma - bloccato da un livello di protezione."""
        phantom = {
            'price_entry':  price,
            'block_reason': block_reason,
            'seed_score':   seed_score,
            'momentum':     momentum,
            'volatility':   volatility,
            'trend':        trend,
            'entry_time':   time.time(),
            'max_price':    price,
            'min_price':    price,
            'regime':       self._regime_current,
            'direction':    self.campo._direction,
        }
        self._phantoms_open.append(phantom)

        # Classifica il blocco per statistiche
        reason_key = block_reason.split("_")[0] if "_" in block_reason else block_reason
        if "DRIFT" in block_reason:    reason_key = "DRIFT_VETO"
        elif "TOSSICO" in block_reason: reason_key = "VETO_TOSSICO"
        elif "LOSS_CONSEC" in block_reason: reason_key = "LOSS_CONSECUTIVI"
        elif "SCORE_SOTTO" in block_reason: reason_key = "SCORE_INSUFFICIENTE"
        elif "ENERGY_BOTH" in block_reason: reason_key = "ENERGY_BOTH"
        elif "ENERGY_SCORE" in block_reason: reason_key = "ENERGY_SCORE"
        elif "ENERGY_TREND" in block_reason: reason_key = "ENERGY_TREND"
        elif "RANGE_MIDZONE" in block_reason: reason_key = "RANGE_MIDZONE"
        elif "OC1" in block_reason: reason_key = "OC1_MIDZONE"
        elif "OC2" in block_reason: reason_key = "OC2_RSI"
        elif "OC3" in block_reason: reason_key = "OC3_DRIFT"
        elif "OC4" in block_reason: reason_key = "OC4_FALSO_FORTE"
        elif "OC5" in block_reason: reason_key = "OC5_LOSS_STREAK"
        elif "CTX_MATCH" in block_reason: reason_key = "CTX_MATCH"
        elif "FANTASMA" in block_reason: reason_key = "FANTASMA"
        else: reason_key = block_reason

        if reason_key not in self._phantom_stats:
            self._phantom_stats[reason_key] = {
                'blocked': 0, 'would_win': 0, 'would_lose': 0,
                'pnl_saved': 0.0, 'pnl_missed': 0.0
            }
        self._phantom_stats[reason_key]['blocked'] += 1

    def _update_phantoms(self, price, momentum):
        """Aggiorna tutti i fantasmi aperti - chiamato ad ogni tick."""
        to_close = []
        for i, ph in enumerate(self._phantoms_open):
            if price > ph['max_price']:
                ph['max_price'] = price
            if price < ph['min_price']:
                ph['min_price'] = price

            duration = time.time() - ph['entry_time']
            # PnL bidirezionale - come il bot reale
            if ph.get('direction', 'LONG') == 'SHORT':
                pnl = ph['price_entry'] - price
            else:
                pnl = price - ph['price_entry']
            pnl_pct = (pnl / ph['price_entry']) * 100

            # -- Stesse regole di uscita del bot reale --
            # Stop loss 2%
            if pnl_pct < -2.0:
                to_close.append((i, price, "HARD_STOP"))
                continue
            # DECEL (semplificato: dopo 15s se in perdita)
            if duration > 15 and pnl < 0:
                to_close.append((i, price, "DECEL_SIM"))
                continue
            # SMORZ - direzione-aware
            if duration > 10:
                if ph.get('direction', 'LONG') == 'LONG' and momentum == "DEBOLE":
                    to_close.append((i, price, "SMORZ_SIM"))
                    continue
                elif ph.get('direction', 'LONG') == 'SHORT' and momentum == "FORTE":
                    to_close.append((i, price, "SMORZ_SIM"))
                    continue
            # WIN takeout (dopo 20s se in profitto, simula DECEL)
            if duration > 20 and pnl > 0:
                to_close.append((i, price, "DECEL_WIN_SIM"))
                continue
            # Timeout 60s
            if duration > 60:
                to_close.append((i, price, "TIMEOUT_SIM"))
                continue

        # Chiudi dal fondo per non rompere gli indici
        for i, close_price, reason in reversed(to_close):
            self._close_phantom(i, close_price, reason)
        
        # -- SHADOW SHORT PHANTOMS - SHORT evitati in RANGING ----------
        if hasattr(self, '_shadow_short_phantoms'):
            to_close_ss = []
            for i, ph in enumerate(self._shadow_short_phantoms):
                if price > ph['max_price']:
                    ph['max_price'] = price
                if price < ph['min_price']:
                    ph['min_price'] = price
                
                duration = time.time() - ph['entry_time']
                # PnL SHORT: guadagna se prezzo scende
                delta = ph['price_entry'] - price
                exposure = self.TRADE_SIZE_USD * self.LEVERAGE
                btc_qty = exposure / ph['price_entry']
                pnl_gross = delta * btc_qty
                pnl = pnl_gross - (exposure * self.FEE_PCT * 2)
                
                close_reason = None
                if pnl < -(self.TRADE_SIZE_USD * 0.02):  # stop loss
                    close_reason = "HARD_STOP_SIM"
                elif duration > 15 and pnl < 0:
                    close_reason = "DECEL_SIM"
                elif duration > 10 and momentum == "FORTE":  # SHORT esce su FORTE
                    close_reason = "SMORZ_SIM"
                elif duration > 20 and pnl > 0:
                    close_reason = "WIN_SIM"
                elif duration > 60:
                    close_reason = "TIMEOUT_SIM"
                
                if close_reason:
                    to_close_ss.append((i, pnl, duration, close_reason))
            
            for i, pnl, dur, reason in reversed(to_close_ss):
                ph = self._shadow_short_phantoms.pop(i)
                if not hasattr(self, '_shadow_short_results'):
                    self._shadow_short_results = deque(maxlen=100)
                self._shadow_short_results.append({
                    'pnl': round(pnl, 2),
                    'duration': round(dur, 1),
                    'is_win': pnl > 0,
                    'exit_reason': reason,
                    'drift': ph['drift'],
                    'macd_hist': ph['macd_hist'],
                    'bearish_energy': ph['bearish_energy'],
                    'price_entry': ph['price_entry'],
                })

    def _close_phantom(self, idx, price, reason):
        """Chiude un fantasma e registra il risultato."""
        try:
            ph = self._phantoms_open.pop(idx)
            # PnL REALE FUTURES - stessa formula dei trade veri
            if ph.get('direction', 'LONG') == 'SHORT':
                delta_price = ph['price_entry'] - price
            else:
                delta_price = price - ph['price_entry']
            exposure = self.TRADE_SIZE_USD * self.LEVERAGE
            btc_qty = exposure / ph['price_entry']
            pnl_gross = delta_price * btc_qty
            total_fees = exposure * self.FEE_PCT * 2
            pnl = pnl_gross - total_fees
            is_win = pnl > 0

            # Aggiorna statistiche per livello di blocco
            block = ph['block_reason']
            reason_key = block.split("_")[0] if "_" in block else block
            if "DRIFT" in block:    reason_key = "DRIFT_VETO"
            elif "TOSSICO" in block: reason_key = "VETO_TOSSICO"
            elif "LOSS_CONSEC" in block: reason_key = "LOSS_CONSECUTIVI"
            elif "SCORE_SOTTO" in block: reason_key = "SCORE_INSUFFICIENTE"
            elif "ENERGY_BOTH" in block: reason_key = "ENERGY_BOTH"
            elif "ENERGY_SCORE" in block: reason_key = "ENERGY_SCORE"
            elif "ENERGY_TREND" in block: reason_key = "ENERGY_TREND"
            elif "RANGE_MIDZONE" in block: reason_key = "RANGE_MIDZONE"
            elif "OC1" in block: reason_key = "OC1_MIDZONE"
            elif "OC2" in block: reason_key = "OC2_RSI"
            elif "OC3" in block: reason_key = "OC3_DRIFT"
            elif "OC4" in block: reason_key = "OC4_FALSO_FORTE"
            elif "OC5" in block: reason_key = "OC5_LOSS_STREAK"
            elif "CTX_MATCH" in block: reason_key = "CTX_MATCH"
            elif "FANTASMA" in block: reason_key = "FANTASMA"
            else: reason_key = block

            if reason_key not in self._phantom_stats:
                self._phantom_stats[reason_key] = {
                    'blocked': 0, 'would_win': 0, 'would_lose': 0,
                    'pnl_saved': 0.0, 'pnl_missed': 0.0
                }

            stats = self._phantom_stats[reason_key]
            if is_win:
                stats['would_win'] += 1
                stats['pnl_missed'] += pnl   # soldi che NON abbiamo guadagnato
            else:
                stats['would_lose'] += 1
                stats['pnl_saved'] += abs(pnl)   # soldi che NON abbiamo perso

            result = {
                'block_reason': block,
                'price_entry':  ph['price_entry'],
                'price_exit':   price,
                'pnl':          round(pnl, 2),
                'is_win':       is_win,
                'exit_reason':  reason,
                'regime':       ph['regime'],
                'direction':    ph.get('direction', 'LONG'),
                'verdict':      "PROTEZIONE" if not is_win else "ZAVORRA",
            }
            self._phantoms_closed.append(result)

            # Log solo se il fantasma è significativo
            _dir = ph.get('direction', 'LONG')
            _dir_tag = "S" if _dir == "SHORT" else "L"
            emoji = "🛡️" if not is_win else "⚠️"
            label = "PROTETTO" if not is_win else "MANCATO"
            ts = datetime.utcnow().strftime('%H:%M:%S')
            log_entry = (f"{ts} {emoji} [PHANTOM {_dir_tag}] {label} ${pnl:+.2f} | "
                        f"bloccato da: {block} | {reason}")
            self._phantom_log.append(log_entry)
            log.info(log_entry)

        except Exception as e:
            log.error(f"[PHANTOM] Errore close: {e}")

    def _get_phantom_summary(self) -> dict:
        """Riepilogo fantasmi per la dashboard."""
        stats = self._phantom_stats
        if not stats:
            return {
                'total': 0, 'protezione': 0, 'zavorra': 0,
                'pnl_saved': 0, 'pnl_missed': 0,
                'verdetto': 'DATI INSUFFICIENTI',
                'per_livello': {},
                'log': list(self._phantom_log),
            }

        # Calcola totali dai dati per livello (COMPLETI, non troncati)
        total_blocked = sum(s['blocked'] for s in stats.values())
        protezione = sum(s['would_lose'] for s in stats.values())
        zavorra = sum(s['would_win'] for s in stats.values())
        pnl_saved = sum(s['pnl_saved'] for s in stats.values())
        pnl_missed = sum(s['pnl_missed'] for s in stats.values())

        if pnl_saved > pnl_missed:
            verdetto = f"PROTEZIONE (+${pnl_saved - pnl_missed:.0f} risparmiati)"
        elif pnl_missed > pnl_saved:
            verdetto = f"ZAVORRA (-${pnl_missed - pnl_saved:.0f} persi in opportunita)"
        else:
            verdetto = "NEUTRO"

        # Energy filter summary - per capire se il problema è score o trend
        energy_keys = ['ENERGY_SCORE', 'ENERGY_TREND', 'ENERGY_BOTH']
        energy_summary = {}
        for ek in energy_keys:
            if ek in stats:
                s = stats[ek]
                total = s['would_win'] + s['would_lose']
                energy_summary[ek] = {
                    'blocked': s['blocked'],
                    'would_win': s['would_win'],
                    'would_lose': s['would_lose'],
                    'pnl_missed': round(s['pnl_missed'], 2),
                    'pnl_saved': round(s['pnl_saved'], 2),
                    'net': round(s['pnl_missed'] - s['pnl_saved'], 2),
                    'wr_simulated': round(s['would_win'] / total * 100, 1) if total > 0 else 0,
                }

        return {
            'total':       total_blocked,
            'protezione':  protezione,
            'zavorra':     zavorra,
            'pnl_saved':   round(pnl_saved, 2),
            'pnl_missed':  round(pnl_missed, 2),
            'bilancio':    round(pnl_saved - pnl_missed, 2),
            'verdetto':    verdetto,
            'per_livello': dict(stats),
            'energy_filter_summary': energy_summary,
            'log':         list(self._phantom_log),
            'open':        len(self._phantoms_open),
        }

    def _read_bridge_commands(self):
        """
        Legge comandi bridge da SQLite (key: bridge_commands) E da bridge_commands.json.
        Protocollo unificato — bridge nuovo scrive su DB, bridge vecchio su file.
        """
        try:
            commands = []
            # -- PROTOCOLLO NUOVO: legge da DB (bridge predittivo V48+) ----
            try:
                conn = sqlite3.connect(DB_PATH)
                rows = conn.execute(
                    "SELECT value FROM bot_state WHERE key='bridge_cmd'"
                ).fetchall()
                conn.close()
                if rows:
                    db_cmds = json.loads(rows[0][0])
                    # Bridge predittivo scrive oggetto singolo {type, data, ts}
                    # Normalizza a lista per compatibilità
                    if isinstance(db_cmds, dict):
                        db_cmds = [db_cmds]
                    if isinstance(db_cmds, list):
                        for cmd in db_cmds:
                            # Normalizza formato bridge predittivo → formato bot
                            if 'type' in cmd and 'data' in cmd and 'executed' not in cmd:
                                cmd['executed'] = False
                        commands.extend(db_cmds)
            except Exception:
                pass
            # -- PROTOCOLLO VECCHIO: legge da file (bridge legacy) ---------
            if os.path.exists(self._bridge_cmd_file):
                with open(self._bridge_cmd_file) as f:
                    file_cmds = json.load(f)
                    if isinstance(file_cmds, list):
                        commands.extend(file_cmds)

            modified = False
            for cmd in commands:
                if cmd.get("executed"):
                    continue

                cmd_type = cmd.get("type", "")
                data     = cmd.get("data", {})

                if cmd_type == "modify_weight":
                    param = data.get("param", "")
                    value = data.get("value")
                    # -- PARAMETRI PROTETTI - calibrati sui dati reali ------
                    # Il bridge NON può toccarli. Solo noi dopo analisi phantom.
                    # Solo i parametri fisici restano protetti
                    # I pesi SC sono gestiti dal Veritas — il bridge può agire
                    PROTECTED_PARAMS = {
                        "SOGLIA_BASE",   # auto-tune la gestisce
                        "SOGLIA_MIN",    # auto-tune la gestisce
                    }
                    if param in PROTECTED_PARAMS:
                        self._log("🌉", f"BRIDGE: RIFIUTATO {param} → {value} (protetto)")
                        ctx = self._tele_ctx()
                        self.telemetry.log_param_rejected(param, value, "protetto_dati_reali", **{k: ctx[k] for k in ('regime','direction','open_position','active_threshold','drift','macd','trend','volatility')})
                        cmd["executed"] = True
                        modified = True
                    elif hasattr(self.campo, param) and value is not None:
                        old = getattr(self.campo, param)
                        setattr(self.campo, param, value)
                        self._log("🌉", f"BRIDGE: {param} {old} → {value}")
                        ctx = self._tele_ctx(bridge_reason=f"modify_weight:{param}")
                        self.telemetry.log_param_change(param, old, value, bridge_reason=f"modify_weight:{param}", **{k: ctx[k] for k in ('regime','direction','open_position','active_threshold','drift','macd','trend','volatility')})
                        cmd["executed"] = True
                        modified = True

                elif cmd_type == "entry_signal":
                    # Bridge predittivo segnala momento di entrata
                    carica = data.get("carica", 0.0)
                    motivo = data.get("motivo", "bridge")
                    self._log("🌉", f"BRIDGE entry_signal — carica={carica:.2f} {motivo}")
                    cmd["executed"] = True
                    modified = True

                elif cmd_type == "adjust_soglia":
                    param = data.get("param", "")
                    value = data.get("value")
                    # -- GUARDRAIL: SOGLIA_BASE è calibrata su 37,112 candele --
                    # Il bridge NON può toccarla. Solo pesi e capsule.
                    if param == "SOGLIA_BASE":
                        self._log("🌉", f"BRIDGE: RIFIUTATO {param} → {value} (protetto da calibrazione storica)")
                        ctx = self._tele_ctx()
                        self.telemetry.log_param_rejected(param, value, "calibrazione_storica", **{k: ctx[k] for k in ('regime','direction','open_position','active_threshold','drift','macd','trend','volatility')})
                        cmd["executed"] = True
                        modified = True
                    elif hasattr(self.campo, param) and value is not None:
                        old = getattr(self.campo, param)
                        setattr(self.campo, param, value)
                        self._log("🌉", f"BRIDGE: {param} {old} → {value}")
                        ctx = self._tele_ctx(bridge_reason=f"adjust_soglia:{param}")
                        self.telemetry.log_param_change(param, old, value, bridge_reason=f"adjust_soglia:{param}", **{k: ctx[k] for k in ('regime','direction','open_position','active_threshold','drift','macd','trend','volatility')})
                        cmd["executed"] = True
                        modified = True

            if modified:
                with open(self._bridge_cmd_file, 'w') as f:
                    json.dump(commands, f, indent=2)

        except Exception as e:
            log.error(f"[BRIDGE_READ] {e}")

    # ========================================================================
    # ORDINI BINANCE (solo LIVE)
    # ========================================================================

    def _place_order(self, side: str, price: float, size_mult: float = 1.0):
        """
        Placeholder per ordini reali su Binance.
        Da completare con python-binance o requests REST API.
        ATTIVO SOLO quando PAPER_TRADE = False.
        """
        log.info(f"[ORDER] 📤 {side} {SYMBOL} @ {price:.2f} size_mult={size_mult:.1f}")
        # TODO: implementa chiamata Binance REST API
        # import requests
        # payload = {"symbol": SYMBOL, "side": side, "type": "MARKET", ...}
        # requests.post("https://api.binance.com/api/v3/order", ...)

    # ========================================================================
    # HEARTBEAT → app.py (Mission Control)
    # ========================================================================

    def _update_heartbeat(self):
        if self.heartbeat_lock:
            self.heartbeat_lock.acquire()
        try:
            if self.heartbeat_data is not None:
                tot = self.wins + self.losses
                self.heartbeat_data.update({
                    "status":          "RUNNING",
                    "mode":            "PAPER" if self.paper_trade else "LIVE",
                    "capital":         round(self.capital, 2),
                    "trades":          self.total_trades,
                    "wins":            self.wins,
                    "losses":          self.losses,
                    "wr":              round(self.wins / tot, 4) if tot > 0 else 0,
                    "last_seen":       datetime.utcnow().isoformat(),
                    "matrimoni_divorzio": list(self.memoria.divorzio),
                    "oracolo_snapshot":   self.oracolo.dump(),
                    "posizione_aperta":   self.trade_open is not None,
                    "live_log":           list(self._live_log),
                    "calibra_params":     self.calibratore.get_params(),
                    "calibra_log":        self.calibratore.get_log(),
                    "regime":             self._regime_current,
                    "regime_conf":        round(self._regime_conf, 3),
                    # -- MOTORE 2: CAMPO GRAVITAZIONALE stats ----------
                    "m2_trades":          self._m2_trades,
                    "m2_wins":            self._m2_wins,
                    "m2_losses":          self._m2_losses,
                    "m2_wr":              round(self._m2_wins / max(1, self._m2_wins + self._m2_losses), 4),
                    "m2_pnl":             round(self._m2_pnl, 4),
                    "m2_shadow_open":     self._shadow is not None,
                    "m2_direction":       self.campo._direction,
                    "m2_entry_price":     round(self._shadow["price_entry"], 4) if self._shadow else 0,
                    "m2_state":           self._state,
                    "m2_loss_streak":     self._m2_loss_streak,
                    "m2_cooldown":        max(0, self._m2_cooldown_until - time.time()),
                    "m2_log":             list(self._m2_log),
                    "m2_campo_stats":     self.campo.get_stats(),
                    "m2_last_score":      round(getattr(self.campo, '_last_score', 0), 1),
                    "m2_last_soglia":     round(getattr(self.campo, '_last_soglia', 60), 1),
                    "m2_buy_distance":    round(getattr(self.campo, '_last_soglia', 60) - getattr(self.campo, '_last_score', 0), 1),
                    "m2_score_components": {
                        "seed":   round(min(1.0,max(0.0,(self.seed_scorer.score().get('score',0)-0.20)/0.60))*25, 1),
                        "fp":     round(getattr(self.campo, '_last_fp_score', 0), 1),
                        "rsi":    round(self.campo._rsi_score()*10, 1),
                        "macd":   round(self.campo._macd_score()*10, 1),
                        "regime": self._regime_current,
                        "warmup_rsi": len(self.campo._prices_ta),
                        "warmup_needed": 35,
                    },
                    "oi_stato":           self._oi_stato,
                    "oi_carica":          round(self._oi_carica, 3),
                    "veritas":            self.veritas.dump_dashboard(),
                    # -- PHANTOM TRACKER - zavorra o protezione? -------
                    "phantom":            self._get_phantom_summary(),
                    # -- INTELLIGENZA AUTONOMA - capsule vive -----------
                    "ia_stats":           (self.capsule_manager.get_stats()
                                           if self.capsule_manager
                                           else self.realtime_engine.get_stats()),
                    # -- PRE-TRADE SIGNAL TRACKER ---------------------------
                    "signal_tracker": {
                        "open":      self.signal_tracker.get_open_count(),
                        "closed":    len(self.signal_tracker._closed),
                        "top":       self.signal_tracker.dump_top(8),
                        "stats_keys": list(self.signal_tracker._stats.keys()),
                        "stats_n":   {k: v['n'] for k,v in self.signal_tracker._stats.items()},
                    },
                    # -- SOGLIA DINAMICA MONITOR -----------------------
                    "m2_soglia_min":      self.campo.SOGLIA_MIN,
                    "m2_soglia_base":     self.campo.SOGLIA_BASE,
                    # -- SHORT EVITATI IN RANGING ----------------------
                    "shadow_short_ranging": self._get_shadow_short_report(),
                    # -- DATI GRANULARI PER BRIDGE (B3) -------------
                    # -- HEARTBEAT ENRICHED telemetria --------
                    "bridge_feed": {
                        "drift_history":   list(self.campo._prices_long)[-20:] and [
                            round((list(self.campo._prices_long)[-20:][i+1] - list(self.campo._prices_long)[-20:][i]) /
                                  max(list(self.campo._prices_long)[-20:][i], 1) * 100, 4)
                            for i in range(len(list(self.campo._prices_long)[-20:])-1)
                        ] if len(self.campo._prices_long) >= 20 else [],
                        "compression_now": round((max(list(self.campo._prices_short)[-5:]) - min(list(self.campo._prices_short)[-5:])) /
                                                  max(max(list(self.campo._prices_short)[-20:]) - min(list(self.campo._prices_short)[-20:]), 0.01), 4)
                                           if len(self.campo._prices_short) >= 20 else 1.0,
                        "seed_history":    list(self.campo._seed_history),
                        "oi_carica":       round(self._oi_carica, 3),
                        "oi_stato":        self._oi_stato,
                        "regime":          self._regime_current,
                        "rsi":             round(self.campo._last_rsi, 1),
                        "macd_hist":       round(self.campo._last_macd_hist, 4),
                        "pb_signals":      self.campo._pre_breakout_factor()[2] if len(self.campo._prices_short) >= 30 else 0,
                    },
                    # -- STABILITY TELEMETRY ------------------------
                    "telemetry":          self.telemetry.generate_report(),
                })
        except Exception as e:
            log.error(f"[HEARTBEAT_ERROR] {e}")
        finally:
            if self.heartbeat_lock:
                self.heartbeat_lock.release()

    def _get_shadow_short_report(self):
        """Report aggregato degli SHORT evitati in RANGING."""
        results = list(getattr(self, '_shadow_short_results', []))
        log_entries = getattr(self, '_shadow_short_log', [])
        
        if not results:
            return {
                'blocked_count': len(log_entries),
                'simulated_count': 0,
                'message': 'Nessun phantom SHORT chiuso ancora',
                'recent_blocked': log_entries[-5:],
            }
        
        wins = [r for r in results if r['is_win']]
        losses = [r for r in results if not r['is_win']]
        total_pnl = sum(r['pnl'] for r in results)
        
        report = {
            'blocked_count': len(log_entries),
            'simulated_count': len(results),
            'would_win': len(wins),
            'would_lose': len(losses),
            'wr_simulated': round(len(wins)/len(results)*100, 1) if results else 0,
            'pnl_total': round(total_pnl, 2),
            'avg_pnl': round(total_pnl / len(results), 2) if results else 0,
            'avg_duration': round(sum(r['duration'] for r in results) / len(results), 1) if results else 0,
            'verdict': 'EDGE' if total_pnl > 0 else 'RUMORE',
            'recent_results': results[-5:],
            'recent_blocked': log_entries[-5:],
        }
        
        by_energy = {}
        for r in results:
            be = r.get('bearish_energy', 0)
            key = f"{be}"
            if key not in by_energy:
                by_energy[key] = {'count': 0, 'pnl': 0, 'wins': 0}
            by_energy[key]['count'] += 1
            by_energy[key]['pnl'] += r['pnl']
            if r['is_win']:
                by_energy[key]['wins'] += 1
        report['by_bearish_energy'] = by_energy
        
        return report

    # ========================================================================
    # RUN
    # ========================================================================

    def _loss_consecutivi(self) -> int:
        """Conta i loss consecutivi dalla coda del log_analyzer."""
        count = 0
        for t in reversed(list(self.log_analyzer.trades)):
            if t.get('pnl', 0) < 0:
                count += 1
            else:
                break
        return count

    def _read_deepseek_commands(self):
        """Legge e applica i comandi DeepSeek da heartbeat_data ogni tick."""
        if not self.heartbeat_data:
            return
        try:
            with self.heartbeat_lock:
                hb = self.heartbeat_data

            now = time.time()

            # ABBASSA_SOGLIA — valida per 60 secondi
            if hb.get("ds_soglia_override"):
                ts = hb.get("ds_soglia_ts", 0)
                if now - ts < 60:
                    val = float(hb["ds_soglia_override"])
                    self.campo._soglia_min_override = val
                    self.campo._soglia_base_override = val
                else:
                    with self.heartbeat_lock:
                        self.heartbeat_data.pop("ds_soglia_override", None)
                        self.heartbeat_data.pop("ds_soglia_ts", None)
                    self.campo._soglia_min_override = None
                    self.campo._soglia_base_override = None

            # RESET_PESI
            if hb.get("ds_reset_pesi"):
                self.supercervello._pesi = dict(self.supercervello.PESI_DEFAULT)
                log.info("[DS] ✅ Pesi SC resettati ai default")
                with self.heartbeat_lock:
                    self.heartbeat_data["ds_reset_pesi"] = False

            # FORZA_ENTRY — valido per 30 secondi
            if hb.get("ds_forza_entry"):
                ts = hb.get("ds_forza_ts", 0)
                if now - ts < 30:
                    self._ds_forza_entry = True
                else:
                    with self.heartbeat_lock:
                        self.heartbeat_data["ds_forza_entry"] = False
                    self._ds_forza_entry = False

            # BLOCCA_SC — valido per 180 secondi (3 minuti)
            if hb.get("ds_blocca_sc"):
                ts = hb.get("ds_blocca_sc_ts", 0)
                if now - ts < 180:
                    self._ds_blocca_sc = True
                else:
                    with self.heartbeat_lock:
                        self.heartbeat_data["ds_blocca_sc"] = False
                    self._ds_blocca_sc = False

        except Exception as e:
            log.warning(f"[DS_CMD] Errore lettura comandi: {e}")

    def run(self):
        log.info("[START] Bot avviato - connessione Binance WS...")
        self.connect_binance()
        try:
            while True:
                time.sleep(1)
                self._read_deepseek_commands()
        except KeyboardInterrupt:
            log.info("[STOP] Bot fermato da utente")
            self._persist.save(self.capital, self.total_trades)

# ===========================================================================
# MAIN (standalone - Render lo avvia tramite bot_launcher.py)
# ===========================================================================

if __name__ == '__main__':
    bot = OvertopBassanoV15Production()
    bot.run()
