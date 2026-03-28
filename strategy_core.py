"""
strategy_core.py — Market discovery + order book metrics + signal engine
Estrategia AI-EDGE: OBI-based signal para mercados Polymarket Up/Down 5m

Variables de entorno:
  SYMBOL = SOL | BTC | ETH  (default: BTC)
"""

import os
import time
import requests
from datetime import datetime, timezone
from py_clob_client.client import ClobClient

CLOB_HOST   = "https://clob.polymarket.com"
GAMMA_API   = "https://gamma-api.polymarket.com"
SLOT_STEP   = 300          # 5 minutos
TOP_LEVELS  = 9999         # todos los niveles del libro

SYMBOL      = os.environ.get("SYMBOL", "BTC").upper()

SLUG_PREFIXES = {
    "SOL": "sol-updown-5m",
    "BTC": "btc-updown-5m",
    "ETH": "eth-updown-5m",
}


# ── Market discovery ──────────────────────────────────────────────────────────

def fetch_gamma_market(slug: str):
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=8)
        r.raise_for_status()
        data = r.json()
        return data[0] if isinstance(data, list) and data else None
    except Exception:
        return None


def fetch_clob_market(condition_id: str):
    try:
        r = requests.get(f"{CLOB_HOST}/markets/{condition_id}", timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def build_market_info(gamma_m, clob_m) -> dict | None:
    tokens = clob_m.get("tokens", [])
    if len(tokens) < 2:
        return None

    up_t   = next((t for t in tokens if "up"   in (t.get("outcome") or "").lower()), tokens[0])
    down_t = next((t for t in tokens if "down" in (t.get("outcome") or "").lower()), tokens[1])

    return {
        "condition_id":     clob_m.get("condition_id"),
        "question":         clob_m.get("question", "Up/Down 5min"),
        "end_date":         gamma_m.get("endDate") or clob_m.get("end_date_iso", ""),
        "market_slug":      clob_m.get("market_slug", ""),
        "accepting_orders": bool(clob_m.get("accepting_orders")),
        "up_token_id":      up_t["token_id"],
        "up_outcome":       up_t.get("outcome", "Up"),
        "up_price":         float(up_t.get("price") or 0.5),
        "down_token_id":    down_t["token_id"],
        "down_outcome":     down_t.get("outcome", "Down"),
        "down_price":       float(down_t.get("price") or 0.5),
    }


def _order_book_live(token_id: str) -> bool:
    try:
        r = requests.get(
            f"{CLOB_HOST}/book",
            params={"token_id": token_id},
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False


def find_active_market(symbol: str) -> dict | None:
    """
    Busca el mercado UP/DOWN 5m activo para el símbolo dado (SOL, BTC, ETH).
    Genera todos los slots posibles alineados a múltiplos de SLOT_STEP.
    """
    slug_prefix = SLUG_PREFIXES.get(symbol.upper())
    if not slug_prefix:
        raise ValueError(f"Símbolo no soportado: {symbol}. Usa SOL, BTC o ETH.")

    now  = int(time.time())
    base = now - (now % SLOT_STEP)

    for offset in [0, 1, -1, 2, -2, -3]:
        ts   = base + offset * SLOT_STEP
        slug = f"{slug_prefix}-{ts}"
        gm   = fetch_gamma_market(slug)
        if not gm:
            continue
        cid = gm.get("conditionId")
        if not cid:
            continue
        cm = fetch_clob_market(cid)
        if not cm:
            continue
        info = build_market_info(gm, cm)
        if not info:
            continue
        if _order_book_live(info["up_token_id"]):
            return info
    return None


def fetch_market_resolution(condition_id: str) -> str | None:
    """Retorna 'UP', 'DOWN', o None si aún no está resuelto."""
    try:
        r = requests.get(f"{GAMMA_API}/markets/{condition_id}", timeout=8)
        r.raise_for_status()
        data = r.json()

        outcome_prices = data.get("outcomePrices")
        if outcome_prices:
            try:
                prices = [float(p) for p in outcome_prices]
                if prices[0] >= 0.99:
                    return "UP"
                elif prices[1] >= 0.99:
                    return "DOWN"
            except Exception:
                pass

        if data.get("resolved"):
            winner = (data.get("winner") or "").lower()
            if "up" in winner:
                return "UP"
            elif "down" in winner:
                return "DOWN"

        return None
    except Exception:
        return None


def seconds_remaining(market_info: dict) -> float | None:
    end_raw = market_info.get("end_date", "")
    if not end_raw:
        return None
    try:
        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        diff   = (end_dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, diff)
    except Exception:
        return None


# ── Order book ────────────────────────────────────────────────────────────────

_clob_client = None


def get_clob_client() -> ClobClient:
    global _clob_client
    if _clob_client is None:
        _clob_client = ClobClient(CLOB_HOST)
    return _clob_client


def get_order_book_metrics(token_id: str, top_n: int = TOP_LEVELS) -> tuple[dict | None, str | None]:
    try:
        ob = get_clob_client().get_order_book(token_id)
    except Exception as e:
        return None, str(e)

    bids = sorted(ob.bids or [], key=lambda x: float(x.price), reverse=True)
    asks = sorted(ob.asks or [], key=lambda x: float(x.price))

    bid_vol = sum(float(b.size) for b in bids)
    ask_vol = sum(float(a.size) for a in asks)
    total   = bid_vol + ask_vol
    obi     = (bid_vol - ask_vol) / total if total > 0 else 0.0

    best_bid = float(bids[0].price) if bids else 0.0
    best_ask = float(asks[0].price) if asks else 0.0
    spread   = round(best_ask - best_bid, 4)

    if total > 0:
        bvwap    = sum(float(b.price) * float(b.size) for b in bids) / bid_vol if bid_vol > 0 else 0
        avwap    = sum(float(a.price) * float(a.size) for a in asks) / ask_vol if ask_vol > 0 else 0
        vwap_mid = (bvwap * bid_vol + avwap * ask_vol) / total
    else:
        vwap_mid = (best_bid + best_ask) / 2

    return {
        "bid_volume":   round(bid_vol, 2),
        "ask_volume":   round(ask_vol, 2),
        "total_volume": round(total, 2),
        "obi":          round(obi, 4),
        "best_bid":     round(best_bid, 4),
        "best_ask":     round(best_ask, 4),
        "spread":       spread,
        "vwap_mid":     round(vwap_mid, 4),
        "num_bids":     len(ob.bids or []),
        "num_asks":     len(ob.asks or []),
        "top_bids":     [(round(float(b.price), 4), round(float(b.size), 2)) for b in bids[:8]],
        "top_asks":     [(round(float(a.price), 4), round(float(a.size), 2)) for a in asks[:8]],
    }, None


# ── Signal engine ─────────────────────────────────────────────────────────────

def compute_signal(obi_now: float, obi_window: list, threshold: float) -> dict:
    avg_obi  = sum(obi_window) / len(obi_window) if obi_window else obi_now
    combined = round(0.6 * obi_now + 0.4 * avg_obi, 4)
    abs_c    = abs(combined)

    if combined > threshold:
        conf  = min(int(50 + (abs_c / 0.5) * 50), 99)
        label = "STRONG UP" if combined > threshold * 2 else "UP"
        color = "green"
    elif combined < -threshold:
        conf  = min(int(50 + (abs_c / 0.5) * 50), 99)
        label = "STRONG DOWN" if combined < -threshold * 2 else "DOWN"
        color = "red"
    else:
        label = "NEUTRAL"
        color = "yellow"
        conf  = 50

    return {
        "label":      label,
        "color":      color,
        "confidence": conf,
        "obi_now":    round(obi_now, 4),
        "obi_avg":    round(avg_obi, 4),
        "combined":   combined,
        "history":    list(obi_window)[-20:],
        "threshold":  threshold,
    }
