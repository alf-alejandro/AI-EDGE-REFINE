"""
hedge_sim.py v3 — Bandas Temporales Dinámicas (AI-EDGE Strategy)
Simulación exacta: ejecución al best_ask, P&L virtual.

Estrategia respaldada por 1164 trades reales:
  - zona precio [0.35-0.65]: genera el 85%+ del PnL positivo
  - zona precio >0.65: WR cae a 36-39%, PnL negativo sistemático
  - hedge L2 en [0.25-0.45]: avg +$0.47/trade vs -$0.17 fuera de rango
  - WINs en RESOLUTION: L1 avg 0.51 vs LOSSes en 0.59

Variables de entorno:
  CAPITAL_INICIAL  float  (default: 100.0)
  STATE_FILE       str    (default: /app/data/state.json)
  LOG_FILE         str    (default: /app/data/hedge_log.json)
  SYMBOL           str    SOL | BTC | ETH (default: BTC)
"""

import os
import sys
import time
import json
import logging
import threading
from datetime import datetime
from collections import deque

from strategy_core import (
    find_active_market,
    get_order_book_metrics,
    compute_signal,
    seconds_remaining,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("hedge_sim")
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ── Configuración ──────────────────────────────────────────────────────────────

CAPITAL_INICIAL = float(os.environ.get("CAPITAL_INICIAL", "100.0"))
STATE_FILE      = os.environ.get("STATE_FILE", "/app/data/state.json")
LOG_FILE        = os.environ.get("LOG_FILE",   "/app/data/hedge_log.json")
SYMBOL          = os.environ.get("SYMBOL", "BTC").upper()

# Comisión taker de Polymarket (fee_rate_bps=1000 en producción = 0.1%)
TAKER_FEE         = 0.001       # 0.1% por orden (buy y sell)

# Parámetros de estrategia AI-EDGE (exactos)
ENTRY_USD         = float(os.environ.get("ENTRY_USD", "3.0"))  # $3 fijo por lado
MIN_USD_ORDEN     = 1.00
POLL_INTERVAL     = 0.5         # segundos entre ciclos

OBI_THRESHOLD     = 0.10
OBI_WINDOW_SIZE   = 8
OBI_STRONG        = 0.20

ENTRY_SECS_MIN     = 60         # no entrar con <60s restantes
PRECIO_L1_MIN      = 0.30
PRECIO_L1_MAX_LATE = 0.52       # banda precio con 60s restantes
PRECIO_L1_MAX_EARLY= 0.65       # banda precio con 240s+ restantes

HEDGE_MOVE_MIN     = 0.05       # L1 debe subir 5c antes de hedgear
HEDGE_OBI_MIN      = -0.05
HEDGE_L2_MIN       = 0.25
HEDGE_L2_MAX_EARLY = 0.45
HEDGE_L2_MAX_LATE  = 0.35

EARLY_EXIT_SECS       = 90      # timeout sin hedge → salir
EARLY_EXIT_OBI_FLIP   = -0.15   # OBI invertido → salir
EARLY_EXIT_PRICE_DROP = 0.12    # caída 12c → salir
EARLY_EXIT_GRACE_SECS = 20      # gracia antes de evaluar early exit
EARLY_EXIT_SECS_MIN   = 45      # <45s sin hedge → salir

COOLDOWN_SECS        = 30
MAX_TRADES_POR_CICLO = 2

RESOLVED_UP_THRESH   = 0.97
RESOLVED_DN_THRESH   = 0.03

# ── Estado global (accesible desde Flask) ─────────────────────────────────────

estado = {
    "capital":      CAPITAL_INICIAL,
    "pnl_total":    0.0,
    "peak_capital": CAPITAL_INICIAL,
    "max_drawdown": 0.0,
    "wins":         0,
    "losses":       0,
    "ciclos":       0,
    "trades":       [],
    "ts_ultimo_trade":   0.0,
    "trades_este_ciclo": 0,
    "running":      False,
    "paused":       False,
}

pos = {
    "activa":           False,
    "lado1_side":       None,
    "lado1_precio":     0.0,
    "lado1_shares":     0.0,
    "lado1_usd":        0.0,
    "lado2_side":       None,
    "lado2_precio":     0.0,
    "lado2_shares":     0.0,
    "lado2_usd":        0.0,
    "hedgeado":         False,
    "capital_usado":    0.0,
    "ts_entrada":       None,
    "secs_entrada":     0.0,
}

obi_history_up = deque(maxlen=OBI_WINDOW_SIZE)
obi_history_dn = deque(maxlen=OBI_WINDOW_SIZE)
eventos        = deque(maxlen=100)

_lock     = threading.Lock()
_stop_evt = threading.Event()
_thread   = None


# ── Bandas temporales dinámicas (AI-EDGE) ─────────────────────────────────────

def banda_entrada_max(secs_restantes: float) -> float:
    """
    Precio máximo para L1 según tiempo restante.
    240s+ → hasta 0.65 | 60s → hasta 0.52 | interpolación lineal.
    """
    secs = max(ENTRY_SECS_MIN, min(240, secs_restantes))
    t = (secs - ENTRY_SECS_MIN) / (240 - ENTRY_SECS_MIN)
    return round(PRECIO_L1_MAX_LATE + t * (PRECIO_L1_MAX_EARLY - PRECIO_L1_MAX_LATE), 4)


def banda_hedge_max(secs_restantes: float) -> float:
    """
    Precio máximo para L2 según tiempo restante.
    >180s → hasta 0.45 | <60s → hasta 0.35.
    """
    t = max(0.0, min(1.0, (secs_restantes - 60) / 120))
    return round(HEDGE_L2_MAX_LATE + t * (HEDGE_L2_MAX_EARLY - HEDGE_L2_MAX_LATE), 4)


def puede_entrar(ask: float, secs_restantes: float) -> tuple:
    if secs_restantes < ENTRY_SECS_MIN:
        return False, f"muy_tarde {secs_restantes:.0f}s < {ENTRY_SECS_MIN}s"
    if ask < PRECIO_L1_MIN:
        return False, f"precio_bajo {ask:.3f} < {PRECIO_L1_MIN}"
    max_ok = banda_entrada_max(secs_restantes)
    if ask > max_ok:
        return False, f"precio_alto {ask:.3f} > {max_ok:.3f} (banda@{secs_restantes:.0f}s)"
    return True, "ok"


def puede_hedgear(ask_l2: float, secs_restantes: float) -> tuple:
    if ask_l2 < HEDGE_L2_MIN:
        return False, f"l2_muy_barato {ask_l2:.3f} < {HEDGE_L2_MIN}"
    max_ok = banda_hedge_max(secs_restantes)
    if ask_l2 > max_ok:
        return False, f"l2_muy_caro {ask_l2:.3f} > {max_ok:.3f} (banda@{secs_restantes:.0f}s)"
    return True, "ok"


# ── Utilidades ────────────────────────────────────────────────────────────────

def mid(m: dict) -> float:
    return (m["best_bid"] + m["best_ask"]) / 2


def log_ev(msg: str):
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _lock:
        eventos.append(line)
    log.info(msg)


def actualizar_drawdown():
    estado["peak_capital"] = max(estado["peak_capital"], estado["capital"])
    dd = estado["peak_capital"] - estado["capital"]
    estado["max_drawdown"] = max(estado["max_drawdown"], dd)


def resetear_pos():
    defaults = {
        "activa": False, "lado1_side": None, "lado1_precio": 0.0,
        "lado1_shares": 0.0, "lado1_usd": 0.0, "lado2_side": None,
        "lado2_precio": 0.0, "lado2_shares": 0.0, "lado2_usd": 0.0,
        "hedgeado": False, "capital_usado": 0.0,
        "ts_entrada": None, "secs_entrada": 0.0,
    }
    for k, v in defaults.items():
        pos[k] = v


def guardar_estado():
    total = estado["wins"] + estado["losses"]
    wr    = estado["wins"] / total * 100 if total > 0 else 0.0
    roi   = (estado["capital"] - CAPITAL_INICIAL) / CAPITAL_INICIAL * 100

    payload = {
        "ts":              datetime.now().isoformat(),
        "capital_inicial": CAPITAL_INICIAL,
        "capital":         round(estado["capital"], 4),
        "pnl_total":       round(estado["pnl_total"], 4),
        "roi":             round(roi, 2),
        "peak_capital":    round(estado["peak_capital"], 4),
        "max_drawdown":    round(estado["max_drawdown"], 4),
        "wins":            estado["wins"],
        "losses":          estado["losses"],
        "win_rate":        round(wr, 1),
        "ciclos":          estado["ciclos"],
        "posicion": {
            "activa":        pos["activa"],
            "lado1":         pos["lado1_side"],
            "lado2":         pos["lado2_side"],
            "hedgeado":      pos["hedgeado"],
            "capital_usado": round(pos["capital_usado"], 4),
            "secs_entrada":  round(pos["secs_entrada"], 0),
        },
        "eventos": list(eventos)[-30:],
        "trades":  estado["trades"][-20:],
    }

    try:
        dirpath = os.path.dirname(STATE_FILE)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        log.warning(f"No se pudo guardar state: {e}")

    try:
        dirpath = os.path.dirname(LOG_FILE)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        log_data = {"summary": payload, "trades": estado["trades"]}
        with open(LOG_FILE, "w") as f:
            json.dump(log_data, f, indent=2)
    except Exception as e:
        log.warning(f"No se pudo guardar log: {e}")


def restaurar_estado():
    try:
        if not os.path.isfile(LOG_FILE):
            return
        with open(LOG_FILE) as f:
            data = json.load(f)
        s = data.get("summary", {})
        estado["capital"]   = float(s.get("capital",   CAPITAL_INICIAL))
        estado["pnl_total"] = float(s.get("pnl_total", 0.0))
        estado["wins"]      = int(s.get("wins",   0))
        estado["losses"]    = int(s.get("losses", 0))
        estado["trades"]    = data.get("trades", [])
        log_ev(f"Estado restaurado — capital=${estado['capital']:.2f} W:{estado['wins']} L:{estado['losses']}")
    except Exception as e:
        log_ev(f"restaurar_estado: {e}")


# ── Lógica de trading (AI-EDGE exacta) ───────────────────────────────────────

def comprar(lado: str, ask: float) -> tuple:
    """Compra simulada al best_ask. Retorna (precio, shares, usd) o (0,0,0).
    Demora 1s para simular latencia de orden real en producción."""
    usd = round(ENTRY_USD, 4)
    if usd < MIN_USD_ORDEN:
        log_ev(f"  ✗ Orden muy pequeña: ${usd:.2f}")
        return 0.0, 0.0, 0.0
    if usd > estado["capital"]:
        log_ev(f"  ✗ Capital insuficiente: ${estado['capital']:.2f}")
        return 0.0, 0.0, 0.0
    log_ev(f"  → Enviando orden {lado} @ {ask:.4f} | ${usd:.2f}...")
    time.sleep(1.0)  # simula latencia de fill en producción
    # Comisión taker 0.1% — reduce shares recibidas (igual que fee_rate_bps=1000 en CLOB)
    shares = round((usd / ask) * (1 - TAKER_FEE), 4)
    fee_usd = round(usd * TAKER_FEE, 6)
    estado["capital"] -= usd
    log_ev(f"  ✓ COMPRA {lado} @ {ask:.4f} | {shares:.4f}sh | ${usd:.2f} (fee ${fee_usd:.4f})")
    return ask, shares, usd


def intentar_entrada(up_m, dn_m, secs):
    if pos["activa"] or secs is None:
        return

    secs_desde_ultimo = time.time() - estado["ts_ultimo_trade"]
    if secs_desde_ultimo < COOLDOWN_SECS:
        return
    if estado["trades_este_ciclo"] >= MAX_TRADES_POR_CICLO:
        return

    obi_up = up_m["obi"]
    obi_dn = dn_m["obi"]

    if obi_up > obi_dn and obi_up >= OBI_THRESHOLD:
        lado = "UP"
        ask  = up_m["best_ask"]
        obi  = obi_up
    elif obi_dn > obi_up and obi_dn >= OBI_THRESHOLD:
        lado = "DOWN"
        ask  = dn_m["best_ask"]
        obi  = obi_dn
    else:
        return

    ok, razon = puede_entrar(ask, secs)
    if not ok:
        log_ev(f"  SKIP entrada {lado} @ {ask:.3f}: {razon}")
        return

    if obi < OBI_STRONG:
        log_ev(f"  OBI débil ({obi:+.3f}) — entrando igual dentro de banda")

    log_ev(
        f"SEÑAL {lado} — OBI={obi:+.3f} | ask={ask:.3f} | "
        f"{int(secs)}s | banda≤{banda_entrada_max(secs):.3f}"
    )

    precio, shares, usd = comprar(lado, ask)
    if usd == 0.0:
        return

    pos["activa"]        = True
    pos["lado1_side"]    = lado
    pos["lado1_precio"]  = precio
    pos["lado1_shares"]  = shares
    pos["lado1_usd"]     = usd
    pos["capital_usado"] = usd
    pos["ts_entrada"]    = time.time()
    pos["secs_entrada"]  = secs

    estado["trades_este_ciclo"] += 1

    log_ev(
        f"ENTRADA LADO1 {lado} @ {precio:.4f} | {shares:.4f}sh | "
        f"${usd:.2f} | {int(secs)}s restantes | cap=${estado['capital']:.2f}"
    )
    guardar_estado()


def intentar_hedge(up_m, dn_m, secs):
    if not pos["activa"] or pos["hedgeado"] or secs is None:
        return

    lado1     = pos["lado1_side"]
    lado2     = "DOWN" if lado1 == "UP" else "UP"
    bid_lado1 = up_m["best_bid"] if lado1 == "UP" else dn_m["best_bid"]
    subida    = bid_lado1 - pos["lado1_precio"]

    if subida < HEDGE_MOVE_MIN:
        return

    obi_lado2 = dn_m["obi"] if lado2 == "DOWN" else up_m["obi"]
    if obi_lado2 < HEDGE_OBI_MIN:
        return

    ask_lado2 = dn_m["best_ask"] if lado2 == "DOWN" else up_m["best_ask"]

    ok, razon = puede_hedgear(ask_lado2, secs)
    if not ok:
        log_ev(f"  SKIP hedge {lado2} @ {ask_lado2:.3f}: {razon}")
        return

    if estado["capital"] * MAX_PCT_POR_LADO < MIN_USD_ORDEN:
        return

    log_ev(
        f"  L1 subió {subida*100:+.1f}c — hedgeando {lado2} @ {ask_lado2:.3f} "
        f"(banda≤{banda_hedge_max(secs):.3f} | {int(secs)}s)"
    )

    precio, shares, usd = comprar(lado2, ask_lado2)
    if usd == 0.0:
        return

    pos["lado2_side"]    = lado2
    pos["lado2_precio"]  = precio
    pos["lado2_shares"]  = shares
    pos["lado2_usd"]     = usd
    pos["hedgeado"]      = True
    pos["capital_usado"] += usd

    log_ev(
        f"HEDGE LADO2 {lado2} @ {precio:.4f} | {shares:.4f}sh | "
        f"${usd:.2f} | cap=${estado['capital']:.2f}"
    )
    guardar_estado()


def intentar_early_exit(up_m, dn_m, secs):
    if not pos["activa"] or pos["hedgeado"]:
        return

    lado1       = pos["lado1_side"]
    bid_lado1   = up_m["best_bid"] if lado1 == "UP" else dn_m["best_bid"]
    obi_lado1   = up_m["obi"]      if lado1 == "UP" else dn_m["obi"]
    secs_en_pos = time.time() - pos["ts_entrada"] if pos["ts_entrada"] else 0
    caida       = pos["lado1_precio"] - bid_lado1

    if secs_en_pos < EARLY_EXIT_GRACE_SECS:
        return

    razon = None

    if secs_en_pos > EARLY_EXIT_SECS:
        razon = f"timeout {int(secs_en_pos)}s sin hedge"
    elif obi_lado1 < EARLY_EXIT_OBI_FLIP:
        razon = f"OBI invertido {obi_lado1:+.3f}"
    elif caida > EARLY_EXIT_PRICE_DROP:
        razon = f"caída {caida*100:.1f}c desde entrada"
    elif secs is not None and secs < EARLY_EXIT_SECS_MIN:
        razon = f"tiempo crítico {int(secs)}s sin hedge"

    if not razon:
        return

    exit_precio = max(bid_lado1, 0.01)
    # Comisión taker 0.1% sobre la venta
    exit_usd = pos["lado1_shares"] * exit_precio * (1 - TAKER_FEE)
    pnl = round(exit_usd - pos["lado1_usd"], 4)

    estado["capital"]   += exit_usd
    estado["pnl_total"] += pnl

    if pnl >= 0:
        estado["wins"] += 1
    else:
        estado["losses"] += 1

    actualizar_drawdown()
    log_ev(
        f"EARLY EXIT {lado1} @ {exit_precio:.4f} | {razon} | "
        f"PnL: ${pnl:+.4f} | cap=${estado['capital']:.2f}"
    )
    _registrar_trade("EARLY_EXIT", exit_precio, None, "WIN" if pnl >= 0 else "LOSS", pnl)
    estado["ts_ultimo_trade"] = time.time()
    resetear_pos()
    guardar_estado()


def verificar_resolucion(up_m, dn_m, secs):
    if not pos["activa"]:
        return

    up_mid = mid(up_m)
    dn_mid = mid(dn_m)

    resuelto = None
    if up_mid >= RESOLVED_UP_THRESH:
        resuelto = "UP"
    elif up_mid <= RESOLVED_DN_THRESH:
        resuelto = "DOWN"
    elif dn_mid >= RESOLVED_UP_THRESH:
        resuelto = "DOWN"
    elif secs is not None and secs <= 0:
        resuelto = "UP" if up_mid > 0.5 else "DOWN"
        log_ev(f"Tiempo agotado — mid UP={up_mid:.3f} → {resuelto}")

    if resuelto:
        _aplicar_resolucion(resuelto)


def _aplicar_resolucion(resuelto: str):
    pnl_total = 0.0
    partes    = []

    if resuelto == pos["lado1_side"]:
        # Comisión taker 0.1% sobre la venta del lado ganador
        venta_l1 = pos["lado1_shares"] * 1.0 * (1 - TAKER_FEE)
        pnl_l1   = round(venta_l1 - pos["lado1_usd"], 4)
        partes.append(f"L1 {pos['lado1_side']}=WIN(${pnl_l1:+.2f})")
    else:
        pnl_l1 = -pos["lado1_usd"]
        partes.append(f"L1 {pos['lado1_side']}=LOSS(${pnl_l1:+.2f})")
    pnl_total += pnl_l1

    if pos["hedgeado"]:
        if resuelto == pos["lado2_side"]:
            venta_l2 = pos["lado2_shares"] * 1.0 * (1 - TAKER_FEE)
            pnl_l2   = round(venta_l2 - pos["lado2_usd"], 4)
            partes.append(f"L2 {pos['lado2_side']}=WIN(${pnl_l2:+.2f})")
        else:
            pnl_l2 = -pos["lado2_usd"]
            partes.append(f"L2 {pos['lado2_side']}=LOSS(${pnl_l2:+.2f})")
        pnl_total += pnl_l2

    estado["capital"]   += pos["capital_usado"] + pnl_total
    estado["pnl_total"] += pnl_total

    outcome = "WIN" if pnl_total >= 0 else "LOSS"
    if outcome == "WIN":
        estado["wins"] += 1
    else:
        estado["losses"] += 1

    actualizar_drawdown()
    log_ev(
        f"RESOLUCIÓN → {resuelto} | {' | '.join(partes)} | "
        f"PnL NETO: ${pnl_total:+.2f} | cap=${estado['capital']:.2f}"
    )
    _registrar_trade(
        "RESOLUTION",
        1.0 if resuelto == pos["lado1_side"] else 0.0,
        resuelto, outcome, pnl_total,
    )
    estado["ts_ultimo_trade"] = time.time()
    resetear_pos()
    guardar_estado()


def _registrar_trade(tipo, exit_precio, resuelto, outcome, pnl):
    estado["trades"].append({
        "ts":           datetime.now().isoformat(),
        "tipo":         tipo,
        "resolucion":   resuelto,
        "lado1_side":   pos["lado1_side"],
        "lado1_usd":    round(pos["lado1_usd"], 4),
        "lado1_precio": round(pos["lado1_precio"], 4),
        "hedgeado":     pos["hedgeado"],
        "lado2_side":   pos["lado2_side"],
        "lado2_usd":    round(pos["lado2_usd"], 4),
        "lado2_precio": round(pos["lado2_precio"], 4),
        "exit_precio":  round(exit_precio, 4),
        "pnl":          round(pnl, 4),
        "capital":      round(estado["capital"], 4),
        "outcome":      outcome,
        "secs_entrada": round(pos["secs_entrada"], 0),
    })


# ── Loop principal ────────────────────────────────────────────────────────────

def _main_loop():
    """Loop de trading — corre en hilo background."""
    log_ev("=" * 70)
    log_ev(f"  HEDGE SIM v3 — Bandas Dinámicas ({SYMBOL})")
    log_ev(f"  Capital: ${CAPITAL_INICIAL:.0f} | Entry fijo: ${ENTRY_USD:.2f}/lado")
    log_ev(f"  Banda entrada: [{PRECIO_L1_MIN:.2f}-{PRECIO_L1_MAX_EARLY:.2f}] → [{PRECIO_L1_MIN:.2f}-{PRECIO_L1_MAX_LATE:.2f}]")
    log_ev(f"  Banda hedge L2: [{HEDGE_L2_MIN:.2f}-{HEDGE_L2_MAX_EARLY:.2f}] → [{HEDGE_L2_MIN:.2f}-{HEDGE_L2_MAX_LATE:.2f}]")
    log_ev("=" * 70)

    restaurar_estado()
    guardar_estado()

    mkt = None

    while not _stop_evt.is_set():
        # Esperar si está pausado
        while estado.get("paused") and not _stop_evt.is_set():
            time.sleep(1)

        if _stop_evt.is_set():
            break

        try:
            if mkt is None:
                log_ev(f"Buscando mercado {SYMBOL} Up/Down 5m...")
                obi_history_up.clear()
                obi_history_dn.clear()
                estado["trades_este_ciclo"] = 0
                mkt = find_active_market(SYMBOL)
                if mkt:
                    estado["ciclos"] += 1
                    log_ev(f"Mercado: {mkt.get('question', '')} | ciclo #{estado['ciclos']}")
                else:
                    log_ev("Sin mercado activo — reintentando en 10s...")
                    time.sleep(10)
                    continue

            up_m, err_up = get_order_book_metrics(mkt["up_token_id"])
            dn_m, err_dn = get_order_book_metrics(mkt["down_token_id"])

            if not up_m or not dn_m:
                log_ev(f"Error OB: {err_up or err_dn}")
                time.sleep(POLL_INTERVAL * 2)
                continue

            secs = seconds_remaining(mkt)

            with _lock:
                obi_history_up.append(up_m["obi"])
                obi_history_dn.append(dn_m["obi"])

            if secs is not None and secs <= 0:
                if pos["activa"]:
                    verificar_resolucion(up_m, dn_m, secs)
                log_ev("Mercado expirado — buscando siguiente ciclo...")
                mkt = None
                time.sleep(5)
                continue

            if pos["activa"]:
                verificar_resolucion(up_m, dn_m, secs)

            if pos["activa"] and not pos["hedgeado"]:
                intentar_early_exit(up_m, dn_m, secs)

            if pos["activa"] and not pos["hedgeado"]:
                intentar_hedge(up_m, dn_m, secs)

            if not pos["activa"]:
                intentar_entrada(up_m, dn_m, secs)

            guardar_estado()

        except Exception as e:
            log_ev(f"Error en loop: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(POLL_INTERVAL)

    log_ev("Bot detenido.")
    estado["running"] = False


# ── Control del hilo ──────────────────────────────────────────────────────────

def start():
    """Inicia el loop de trading en background."""
    global _thread
    if estado["running"]:
        return False, "Ya está corriendo"
    _stop_evt.clear()
    estado["running"] = True
    estado["paused"]  = False
    _thread = threading.Thread(target=_main_loop, daemon=True, name="hedge-sim")
    _thread.start()
    log_ev("Bot INICIADO")
    return True, "ok"


def stop():
    """Detiene el loop de trading."""
    if not estado["running"]:
        return False, "No está corriendo"
    _stop_evt.set()
    estado["running"] = False
    estado["paused"]  = False
    log_ev("Bot DETENIDO")
    guardar_estado()
    return True, "ok"


def get_state_snapshot() -> dict:
    """Snapshot del estado actual para la API."""
    total = estado["wins"] + estado["losses"]
    wr    = estado["wins"] / total * 100 if total > 0 else 0.0
    roi   = (estado["capital"] - CAPITAL_INICIAL) / CAPITAL_INICIAL * 100

    with _lock:
        evs    = list(eventos)
        trades = list(estado["trades"])

    return {
        "ts":              datetime.now().isoformat(),
        "capital_inicial": round(CAPITAL_INICIAL, 2),
        "capital":         round(estado["capital"], 4),
        "pnl_total":       round(estado["pnl_total"], 4),
        "roi":             round(roi, 2),
        "peak_capital":    round(estado["peak_capital"], 4),
        "max_drawdown":    round(estado["max_drawdown"], 4),
        "wins":            estado["wins"],
        "losses":          estado["losses"],
        "win_rate":        round(wr, 1),
        "ciclos":          estado["ciclos"],
        "running":         estado["running"],
        "paused":          estado["paused"],
        "symbol":          SYMBOL,
        "posicion": {
            "activa":        pos["activa"],
            "lado1":         pos["lado1_side"],
            "lado2":         pos["lado2_side"],
            "hedgeado":      pos["hedgeado"],
            "capital_usado": round(pos["capital_usado"], 4),
            "secs_entrada":  round(pos["secs_entrada"], 0),
            "lado1_precio":  round(pos["lado1_precio"], 4),
            "lado2_precio":  round(pos["lado2_precio"], 4),
        },
        "eventos": evs[-40:],
        "trades":  trades[-50:],
    }


if __name__ == "__main__":
    # Ejecutar directamente sin Flask
    try:
        _main_loop()
    except KeyboardInterrupt:
        guardar_estado()
        total = estado["wins"] + estado["losses"]
        wr    = estado["wins"] / total * 100 if total > 0 else 0
        roi   = (estado["capital"] - CAPITAL_INICIAL) / CAPITAL_INICIAL * 100
        print(f"\nCapital: ${estado['capital']:.2f} | ROI: {roi:+.1f}% | W:{estado['wins']} L:{estado['losses']} WR:{wr:.0f}%")
