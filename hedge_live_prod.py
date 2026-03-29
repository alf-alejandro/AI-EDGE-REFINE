"""
hedge_live_prod.py v3 — Producción: órdenes reales en Polymarket CLOB
Misma estrategia AI-EDGE exacta, pero con ejecución real de órdenes.

IMPORTANTE: Arranca en modo PAUSADO. Activar vía /api/start o dashboard.

Variables de entorno requeridas:
  POLYMARKET_KEY   str   Clave privada hex
  PROXY_ADDRESS    str   Wallet proxy Polymarket
  CAPITAL_INICIAL  float (default: 100.0)
  ENTRY_USD        float (default: 1.50)  — USD por lado por trade
  SYMBOL           str   BTC | SOL | ETH  (default: BTC)
  STATE_FILE       str   (default: /app/data/state.json)
  LOG_FILE         str   (default: /app/data/hedge_log.json)
"""

import os
import sys
import time
import json
import logging
import threading
from datetime import datetime
from collections import deque

import strategy_core_prod as core
from ws_client import MarketDataWS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("hedge_prod")
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ── Configuración ──────────────────────────────────────────────────────────────

CAPITAL_INICIAL = float(os.environ.get("CAPITAL_INICIAL", "100.0"))
ENTRY_USD       = float(os.environ.get("ENTRY_USD", "1.50"))
STATE_FILE      = os.environ.get("STATE_FILE", "/app/data/state.json")
LOG_FILE        = os.environ.get("LOG_FILE",   "/app/data/hedge_log.json")
SYMBOL          = os.environ.get("SYMBOL", "BTC").upper()

# Parámetros AI-EDGE (exactos — no cambiar sin validar con datos)
MIN_USD_ORDEN     = 1.00
POLL_INTERVAL     = 0.2         # límite API: 150 req/s, usamos ~10 req/s

OBI_THRESHOLD     = 0.10
OBI_WINDOW_SIZE   = 8
OBI_STRONG        = 0.20

ENTRY_SECS_MIN     = 60
PRECIO_L1_MIN      = 0.30
PRECIO_L1_MAX_LATE = 0.52
PRECIO_L1_MAX_EARLY= 0.65

HEDGE_MOVE_MIN     = 0.05
HEDGE_OBI_MIN      = -0.05
HEDGE_L2_MIN       = 0.25
HEDGE_L2_MAX_EARLY = 0.45
HEDGE_L2_MAX_LATE  = 0.35

EARLY_EXIT_SECS       = 90
EARLY_EXIT_OBI_FLIP   = -0.15
EARLY_EXIT_PRICE_DROP = 0.12
EARLY_EXIT_GRACE_SECS = 20
EARLY_EXIT_SECS_MIN   = 45

COOLDOWN_SECS        = 30
MAX_TRADES_POR_CICLO = 2

RESOLVED_UP_THRESH   = 0.97
RESOLVED_DN_THRESH   = 0.03

# ── Estado global ─────────────────────────────────────────────────────────────

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
    "paused":       True,   # PROD arranca pausado
}

pos = {
    "activa":            False,
    "lado1_side":        None,
    "lado1_precio":      0.0,
    "lado1_shares":      0.0,
    "lado1_usd":         0.0,
    "lado1_token_id":    None,
    "lado2_side":        None,
    "lado2_precio":      0.0,
    "lado2_shares":      0.0,
    "lado2_usd":         0.0,
    "lado2_token_id":    None,
    "hedgeado":          False,
    "capital_usado":     0.0,
    "ts_entrada":        None,
    "secs_entrada":      0.0,
}

obi_history_up = deque(maxlen=OBI_WINDOW_SIZE)
obi_history_dn = deque(maxlen=OBI_WINDOW_SIZE)
eventos        = deque(maxlen=100)

_lock     = threading.Lock()
_stop_evt = threading.Event()
_thread   = None


# ── Bandas temporales (AI-EDGE exactas) ──────────────────────────────────────

def banda_entrada_max(secs_restantes: float) -> float:
    secs = max(ENTRY_SECS_MIN, min(240, secs_restantes))
    t = (secs - ENTRY_SECS_MIN) / (240 - ENTRY_SECS_MIN)
    return round(PRECIO_L1_MAX_LATE + t * (PRECIO_L1_MAX_EARLY - PRECIO_L1_MAX_LATE), 4)


def banda_hedge_max(secs_restantes: float) -> float:
    t = max(0.0, min(1.0, (secs_restantes - 60) / 120))
    return round(HEDGE_L2_MAX_LATE + t * (HEDGE_L2_MAX_EARLY - HEDGE_L2_MAX_LATE), 4)


def puede_entrar(ask: float, secs_restantes: float) -> tuple:
    if secs_restantes < ENTRY_SECS_MIN:
        return False, f"muy_tarde {secs_restantes:.0f}s"
    if ask < PRECIO_L1_MIN:
        return False, f"precio_bajo {ask:.3f}"
    max_ok = banda_entrada_max(secs_restantes)
    if ask > max_ok:
        return False, f"precio_alto {ask:.3f} > {max_ok:.3f}"
    return True, "ok"


def puede_hedgear(ask_l2: float, secs_restantes: float) -> tuple:
    if ask_l2 < HEDGE_L2_MIN:
        return False, f"l2_muy_barato {ask_l2:.3f}"
    max_ok = banda_hedge_max(secs_restantes)
    if ask_l2 > max_ok:
        return False, f"l2_muy_caro {ask_l2:.3f} > {max_ok:.3f}"
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
        "lado1_shares": 0.0, "lado1_usd": 0.0, "lado1_token_id": None,
        "lado2_side": None, "lado2_precio": 0.0, "lado2_shares": 0.0,
        "lado2_usd": 0.0, "lado2_token_id": None, "hedgeado": False,
        "capital_usado": 0.0, "ts_entrada": None, "secs_entrada": 0.0,
    }
    for k, v in defaults.items():
        pos[k] = v


def guardar_estado():
    total = estado["wins"] + estado["losses"]
    wr    = estado["wins"] / total * 100 if total > 0 else 0.0
    roi   = (estado["capital"] - CAPITAL_INICIAL) / CAPITAL_INICIAL * 100

    payload = {
        "ts":              datetime.now().isoformat(),
        "modo":            "PRODUCCION",
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

    for path in [STATE_FILE, LOG_FILE]:
        try:
            dirpath = os.path.dirname(path)
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
            with open(path, "w") as f:
                json.dump(
                    payload if path == STATE_FILE else {"summary": payload, "trades": estado["trades"]},
                    f, indent=2
                )
        except Exception as e:
            log.warning(f"No se pudo guardar {path}: {e}")


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


# ── Ejecución de órdenes REALES ───────────────────────────────────────────────

def comprar_real(lado: str, token_id: str, ask: float) -> tuple:
    """Coloca orden de compra real. Retorna (precio, shares, usd) o (0,0,0)."""
    usd = round(ENTRY_USD, 4)
    if usd < MIN_USD_ORDEN:
        log_ev(f"  ✗ ENTRY_USD muy pequeño: ${usd:.2f}")
        return 0.0, 0.0, 0.0
    if usd > estado["capital"]:
        log_ev(f"  ✗ Capital insuficiente: ${estado['capital']:.2f}")
        return 0.0, 0.0, 0.0

    log_ev(f"  → ORDEN REAL BUY {lado} @ {ask:.4f} | ${usd:.2f}")
    result = core.place_taker_buy(token_id, usd, max_price=ask + 0.02)

    if not result["filled"]:
        log_ev(f"  ✗ Orden no llenada ({lado})")
        return 0.0, 0.0, 0.0

    estado["capital"] -= result["usd"]
    log_ev(f"  ✓ Compra REAL {lado} @ {result['price']:.4f} | {result['shares']:.4f}sh | ${result['usd']:.2f}")
    return result["price"], result["shares"], result["usd"]


def vender_real(lado: str, token_id: str, shares: float, bid: float) -> float:
    """Coloca orden de venta real. Retorna usd recibido (0 si falló)."""
    if shares < 0.01:
        return 0.0

    log_ev(f"  → ORDEN REAL SELL {lado} | {shares:.4f}sh @ {bid:.4f}")
    result = core.place_taker_sell(token_id, shares, min_price=0.01)

    if not result["filled"]:
        log_ev(f"  ✗ Venta no ejecutada ({lado})")
        return 0.0

    log_ev(f"  ✓ Venta REAL {lado} @ {result['price']:.4f} = ${result['usd']:.2f}")
    return result["usd"]


# ── Lógica de trading (AI-EDGE exacta) ───────────────────────────────────────

def intentar_entrada(up_m, dn_m, secs, mkt):
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
        lado     = "UP"
        ask      = up_m["best_ask"]
        obi      = obi_up
        token_id = mkt["up_token_id"]
    elif obi_dn > obi_up and obi_dn >= OBI_THRESHOLD:
        lado     = "DOWN"
        ask      = dn_m["best_ask"]
        obi      = obi_dn
        token_id = mkt["down_token_id"]
    else:
        return

    ok, razon = puede_entrar(ask, secs)
    if not ok:
        log_ev(f"  SKIP entrada {lado} @ {ask:.3f}: {razon}")
        return

    log_ev(
        f"SEÑAL {lado} — OBI={obi:+.3f} | ask={ask:.3f} | "
        f"{int(secs)}s | banda≤{banda_entrada_max(secs):.3f}"
    )

    # Pre-aprobar token
    core.approve_conditional_token(token_id)

    precio, shares, usd = comprar_real(lado, token_id, ask)
    if usd == 0.0:
        return

    pos["activa"]         = True
    pos["lado1_side"]     = lado
    pos["lado1_precio"]   = precio
    pos["lado1_shares"]   = shares
    pos["lado1_usd"]      = usd
    pos["lado1_token_id"] = token_id
    pos["capital_usado"]  = usd
    pos["ts_entrada"]     = time.time()
    pos["secs_entrada"]   = secs

    estado["trades_este_ciclo"] += 1

    log_ev(
        f"ENTRADA LADO1 {lado} @ {precio:.4f} | {shares:.4f}sh | "
        f"${usd:.2f} | {int(secs)}s restantes | cap=${estado['capital']:.2f}"
    )
    guardar_estado()


def intentar_hedge(up_m, dn_m, secs, mkt):
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

    ask_lado2  = dn_m["best_ask"] if lado2 == "DOWN" else up_m["best_ask"]
    token_id_2 = mkt["down_token_id"] if lado2 == "DOWN" else mkt["up_token_id"]

    ok, razon = puede_hedgear(ask_lado2, secs)
    if not ok:
        log_ev(f"  SKIP hedge {lado2} @ {ask_lado2:.3f}: {razon}")
        return

    log_ev(
        f"  L1 subió {subida*100:+.1f}c — hedgeando {lado2} @ {ask_lado2:.3f} "
        f"(banda≤{banda_hedge_max(secs):.3f} | {int(secs)}s)"
    )

    core.approve_conditional_token(token_id_2)
    precio, shares, usd = comprar_real(lado2, token_id_2, ask_lado2)
    if usd == 0.0:
        return

    pos["lado2_side"]     = lado2
    pos["lado2_precio"]   = precio
    pos["lado2_shares"]   = shares
    pos["lado2_usd"]      = usd
    pos["lado2_token_id"] = token_id_2
    pos["hedgeado"]       = True
    pos["capital_usado"]  += usd

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

    exit_usd = vender_real(lado1, pos["lado1_token_id"], pos["lado1_shares"], bid_lado1)
    if exit_usd == 0.0:
        # Fallback: precio estimado al bid actual
        exit_usd = pos["lado1_shares"] * max(bid_lado1, 0.01)

    pnl = round(exit_usd - pos["lado1_usd"], 4)
    estado["capital"]   += exit_usd
    estado["pnl_total"] += pnl

    if pnl >= 0:
        estado["wins"] += 1
    else:
        estado["losses"] += 1

    actualizar_drawdown()
    log_ev(
        f"EARLY EXIT {lado1} @ {bid_lado1:.4f} | {razon} | "
        f"PnL: ${pnl:+.4f} | cap=${estado['capital']:.2f}"
    )
    _registrar_trade("EARLY_EXIT", bid_lado1, None, "WIN" if pnl >= 0 else "LOSS", pnl)
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
        _aplicar_resolucion(resuelto, up_m, dn_m)


def _aplicar_resolucion(resuelto: str, up_m, dn_m):
    pnl_total = 0.0
    partes    = []

    # Vender L1 si ganó
    if resuelto == pos["lado1_side"]:
        bid_l1    = up_m["best_bid"] if pos["lado1_side"] == "UP" else dn_m["best_bid"]
        usd_rec   = vender_real(pos["lado1_side"], pos["lado1_token_id"], pos["lado1_shares"], bid_l1)
        if usd_rec == 0.0:
            usd_rec = pos["lado1_shares"] * 1.0  # fallback: resolución a $1
        pnl_l1  = usd_rec - pos["lado1_usd"]
        estado["capital"] += usd_rec
        partes.append(f"L1 {pos['lado1_side']}=WIN(${pnl_l1:+.2f})")
    else:
        pnl_l1 = -pos["lado1_usd"]
        partes.append(f"L1 {pos['lado1_side']}=LOSS(${pnl_l1:+.2f})")
    pnl_total += pnl_l1

    # Vender L2 si ganó
    if pos["hedgeado"]:
        if resuelto == pos["lado2_side"]:
            bid_l2    = dn_m["best_bid"] if pos["lado2_side"] == "DOWN" else up_m["best_bid"]
            usd_rec_2 = vender_real(pos["lado2_side"], pos["lado2_token_id"], pos["lado2_shares"], bid_l2)
            if usd_rec_2 == 0.0:
                usd_rec_2 = pos["lado2_shares"] * 1.0
            pnl_l2  = usd_rec_2 - pos["lado2_usd"]
            estado["capital"] += usd_rec_2
            partes.append(f"L2 {pos['lado2_side']}=WIN(${pnl_l2:+.2f})")
        else:
            pnl_l2 = -pos["lado2_usd"]
            partes.append(f"L2 {pos['lado2_side']}=LOSS(${pnl_l2:+.2f})")
        pnl_total += pnl_l2

    estado["pnl_total"] += pnl_total

    # Sincronizar capital real desde CLOB
    try:
        real_bal = core.get_usdc_balance()
        if real_bal > 0:
            estado["capital"] = real_bal
            log_ev(f"Balance USDC real sincronizado: ${real_bal:.2f}")
    except Exception as e:
        log.warning(f"No se pudo sincronizar balance: {e}")

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
    log_ev("=" * 70)
    log_ev(f"  HEDGE PROD v3 — Bandas Dinámicas ({SYMBOL}) [PRODUCCION REAL]")
    log_ev(f"  Capital inicial: ${CAPITAL_INICIAL:.0f} | Entry: ${ENTRY_USD:.2f}/lado")
    log_ev(f"  *** ARRANCA PAUSADO — activar vía /api/start ***")
    log_ev("=" * 70)

    restaurar_estado()

    # Sincronizar capital real al inicio
    try:
        real_bal = core.get_usdc_balance()
        if real_bal > 0:
            estado["capital"] = real_bal
            log_ev(f"Balance USDC inicial: ${real_bal:.2f}")
    except Exception as e:
        log_ev(f"No se pudo obtener balance inicial: {e}")

    guardar_estado()

    mkt    = None
    mkt_ws = MarketDataWS()   # WebSocket de order book en tiempo real

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
                mkt_ws.unsubscribe()
                mkt = core.find_active_market(SYMBOL)
                if mkt:
                    estado["ciclos"] += 1
                    log_ev(f"Mercado: {mkt.get('question', '')} | ciclo #{estado['ciclos']}")
                    # Suscribir WebSocket al nuevo mercado
                    mkt_ws.subscribe([mkt["up_token_id"], mkt["down_token_id"]])
                    log_ev("MarketWS suscrito — order book en tiempo real")
                else:
                    log_ev("Sin mercado activo — reintentando en 10s...")
                    time.sleep(10)
                    continue

            # ── Obtener order book: WebSocket primero, fallback REST ──────────
            up_m = mkt_ws.get_metrics(mkt["up_token_id"])
            dn_m = mkt_ws.get_metrics(mkt["down_token_id"])

            if not up_m or not dn_m:
                # Todavía no llegó el snapshot WS — usar REST
                up_m, err_up = core.get_order_book_metrics(mkt["up_token_id"])
                dn_m, err_dn = core.get_order_book_metrics(mkt["down_token_id"])
                if not up_m or not dn_m:
                    log_ev(f"Error OB: {err_up or err_dn}")
                    time.sleep(POLL_INTERVAL * 2)
                    continue

            secs = core.seconds_remaining(mkt)

            with _lock:
                obi_history_up.append(up_m["obi"])
                obi_history_dn.append(dn_m["obi"])

            if secs is not None and secs <= 0:
                if pos["activa"]:
                    verificar_resolucion(up_m, dn_m, secs)
                log_ev("Mercado expirado — buscando siguiente ciclo...")
                mkt_ws.unsubscribe()
                mkt = None
                time.sleep(5)
                continue

            if pos["activa"]:
                verificar_resolucion(up_m, dn_m, secs)

            if pos["activa"] and not pos["hedgeado"]:
                intentar_early_exit(up_m, dn_m, secs)

            if pos["activa"] and not pos["hedgeado"]:
                intentar_hedge(up_m, dn_m, secs, mkt)

            if not pos["activa"]:
                intentar_entrada(up_m, dn_m, secs, mkt)

            guardar_estado()

        except Exception as e:
            log_ev(f"Error en loop: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(POLL_INTERVAL)

    log_ev("Bot producción detenido.")
    estado["running"] = False


# ── Control del hilo ──────────────────────────────────────────────────────────

def start():
    global _thread
    if estado["running"]:
        return False, "Ya está corriendo"
    _stop_evt.clear()
    estado["running"] = True
    estado["paused"]  = False
    _thread = threading.Thread(target=_main_loop, daemon=True, name="hedge-prod")
    _thread.start()
    log_ev("Bot INICIADO [MODO PRODUCCION]")
    return True, "ok"


def stop():
    if not estado["running"]:
        return False, "No está corriendo"
    _stop_evt.set()
    estado["running"] = False
    estado["paused"]  = False
    log_ev("Bot DETENIDO")
    guardar_estado()
    return True, "ok"


def get_state_snapshot() -> dict:
    total = estado["wins"] + estado["losses"]
    wr    = estado["wins"] / total * 100 if total > 0 else 0.0
    roi   = (estado["capital"] - CAPITAL_INICIAL) / CAPITAL_INICIAL * 100

    with _lock:
        evs    = list(eventos)
        trades = list(estado["trades"])

    return {
        "ts":              datetime.now().isoformat(),
        "modo":            "PRODUCCION",
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
        "entry_usd":       ENTRY_USD,
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
