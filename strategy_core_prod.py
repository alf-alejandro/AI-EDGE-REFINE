"""
strategy_core_prod.py — Market discovery + CLOB autenticado + ejecución de órdenes
Versión producción: órdenes reales en Polymarket via CLOB API.

Variables de entorno requeridas:
  POLYMARKET_KEY   str   Clave privada hex (sin 0x)
  PROXY_ADDRESS    str   Wallet proxy de Polymarket
  CHAIN_ID         int   137 = Polygon (default)
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, MarketOrderArgs
from py_clob_client.constants import AMOY

log = logging.getLogger("strategy_core_prod")

CLOB_HOST  = "https://clob.polymarket.com"
GAMMA_API  = "https://gamma-api.polymarket.com"
SLOT_STEP  = 300

POLYMARKET_KEY = os.environ.get("POLYMARKET_KEY", "")
PROXY_ADDRESS  = os.environ.get("PROXY_ADDRESS", "")
CHAIN_ID       = int(os.environ.get("CHAIN_ID", "137"))

SLUG_PREFIXES = {
    "SOL": "sol-updown-5m",
    "BTC": "btc-updown-5m",
    "ETH": "eth-updown-5m",
}

_auth_client = None


# ── Cliente autenticado ───────────────────────────────────────────────────────

def get_authenticated_clob_client() -> ClobClient:
    global _auth_client
    if _auth_client is None:
        if not POLYMARKET_KEY:
            raise EnvironmentError("POLYMARKET_KEY no configurado")
        _auth_client = ClobClient(
            host=CLOB_HOST,
            key=POLYMARKET_KEY,
            chain_id=CHAIN_ID,
            signature_type=0,
            funder=PROXY_ADDRESS or None,
        )
        _auth_client.set_api_creds(_auth_client.create_or_derive_api_creds())
        log.info("Cliente CLOB autenticado OK")
    return _auth_client


# ── Balance ───────────────────────────────────────────────────────────────────

def get_usdc_balance() -> float:
    """Obtiene balance USDC real con métodos en cascada."""
    # Método 1: CLOB client
    try:
        client = get_authenticated_clob_client()
        bal = client.get_balance()
        if bal is not None:
            return float(bal)
    except Exception as e:
        log.warning(f"get_usdc_balance (clob): {e}")

    # Método 2: API REST directa
    try:
        client = get_authenticated_clob_client()
        headers = client.create_l1_headers("GET", "/balance")
        r = requests.get(f"{CLOB_HOST}/balance", headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            return float(data.get("balance", 0))
    except Exception as e:
        log.warning(f"get_usdc_balance (l1): {e}")

    return 0.0


def get_clob_balance(token_id: str) -> float:
    """Obtiene balance de tokens condicionales en CLOB."""
    try:
        client = get_authenticated_clob_client()
        bal = client.get_balance(token_id=token_id)
        return float(bal) if bal is not None else 0.0
    except Exception as e:
        log.warning(f"get_clob_balance({token_id[:8]}...): {e}")
        return 0.0


def approve_conditional_token(token_id: str) -> bool:
    """Aprueba token condicional para trading."""
    try:
        client = get_authenticated_clob_client()
        result = client.update_balance_allowance(token_id=token_id)
        log.info(f"Token aprobado: {token_id[:16]}...")
        return True
    except Exception as e:
        log.warning(f"approve_conditional_token: {e}")
        return False


# ── Ejecución de órdenes ──────────────────────────────────────────────────────

def place_taker_buy(token_id: str, usd_amount: float, max_price: float = 0.99) -> dict:
    """
    Compra de mercado (taker). Sondea el fill por 4 segundos.
    Retorna {"filled": True/False, "price": float, "shares": float, "usd": float}
    """
    result = {"filled": False, "price": 0.0, "shares": 0.0, "usd": 0.0, "order_id": None}

    if usd_amount < 1.0:
        log.warning(f"place_taker_buy: monto muy pequeño ${usd_amount:.2f}")
        return result

    try:
        client = get_authenticated_clob_client()

        # Obtener best_ask actual
        ob = client.get_order_book(token_id)
        asks = sorted(ob.asks or [], key=lambda x: float(x.price))
        if not asks:
            log.warning("place_taker_buy: libro vacío")
            return result

        best_ask = float(asks[0].price)
        if best_ask > max_price:
            log.warning(f"place_taker_buy: ask={best_ask:.4f} > max={max_price:.4f}")
            return result

        shares = round(usd_amount / best_ask, 2)

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=usd_amount,
        )
        resp = client.create_market_order(order_args)
        order_id = resp.get("orderID") or resp.get("id", "")
        result["order_id"] = order_id

        log.info(f"Orden buy enviada: {order_id} | ask={best_ask:.4f} | ${usd_amount:.2f}")

        # Sondear fill por 3 segundos (0.25s × 12 — límite POST: 60 req/s)
        for _ in range(12):
            time.sleep(0.25)
            try:
                order = client.get_order(order_id)
                status = (order.get("status") or "").lower()
                size_matched = float(order.get("size_matched") or 0)
                avg_price    = float(order.get("avg_price") or best_ask)

                if status in ("matched", "filled") or size_matched > 0:
                    result["filled"] = True
                    result["price"]  = avg_price
                    result["shares"] = size_matched if size_matched > 0 else shares
                    result["usd"]    = round(result["shares"] * avg_price, 4)
                    log.info(f"Buy FILLED: {result['shares']:.4f}sh @ {avg_price:.4f} = ${result['usd']:.2f}")
                    return result
            except Exception:
                pass

        # Cancelar si no se llenó
        try:
            client.cancel(order_id)
            log.warning(f"Buy order cancelada (no llenada): {order_id}")
        except Exception:
            pass

    except Exception as e:
        log.error(f"place_taker_buy error: {e}")

    return result


def place_taker_sell(token_id: str, shares: float, min_price: float = 0.01) -> dict:
    """
    Venta de mercado (taker). Hasta 5 reintentos con descuento progresivo en bid.
    Retorna {"filled": True/False, "price": float, "usd": float}
    """
    result = {"filled": False, "price": 0.0, "usd": 0.0}

    if shares < 0.01:
        log.warning(f"place_taker_sell: shares muy pequeñas {shares:.4f}")
        return result

    client = get_authenticated_clob_client()

    for attempt in range(5):
        try:
            ob = client.get_order_book(token_id)
            bids = sorted(ob.bids or [], key=lambda x: float(x.price), reverse=True)
            if not bids:
                log.warning(f"place_taker_sell attempt {attempt+1}: libro vacío")
                time.sleep(1)
                continue

            # Descuento progresivo: 0¢, 0.5¢, 1¢, 1.5¢, 2¢
            discount = attempt * 0.005
            bid_price = max(float(bids[0].price) - discount, min_price)

            order_args = OrderArgs(
                token_id=token_id,
                price=bid_price,
                size=shares,
                side="SELL",
            )
            resp = client.create_order(order_args, options={"orderType": OrderType.GTC})
            order_id = resp.get("orderID") or resp.get("id", "")

            log.info(f"Sell attempt {attempt+1}: {order_id} | bid={bid_price:.4f} | {shares:.4f}sh")

            # Sondear fill por 4 segundos (0.25s × 16)
            for _ in range(16):
                time.sleep(0.25)
                try:
                    order = client.get_order(order_id)
                    status = (order.get("status") or "").lower()
                    size_matched = float(order.get("size_matched") or 0)
                    avg_price    = float(order.get("avg_price") or bid_price)

                    if status in ("matched", "filled") or size_matched > 0:
                        result["filled"] = True
                        result["price"]  = avg_price
                        result["usd"]    = round(size_matched * avg_price if size_matched > 0 else shares * avg_price, 4)
                        log.info(f"Sell FILLED: {shares:.4f}sh @ {avg_price:.4f} = ${result['usd']:.2f}")
                        return result
                except Exception:
                    pass

            # Cancelar y reintentar
            try:
                client.cancel(order_id)
            except Exception:
                pass

        except Exception as e:
            log.error(f"place_taker_sell attempt {attempt+1} error: {e}")
            time.sleep(1)

    log.error(f"place_taker_sell: no se pudo vender {shares:.4f}sh después de 5 intentos")
    return result


# ── Market discovery (igual que simulación) ───────────────────────────────────

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
        r = requests.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def find_active_market(symbol: str) -> dict | None:
    slug_prefix = SLUG_PREFIXES.get(symbol.upper())
    if not slug_prefix:
        raise ValueError(f"Símbolo no soportado: {symbol}")

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


def get_order_book_metrics(token_id: str) -> tuple:
    try:
        client = get_authenticated_clob_client()
        ob = client.get_order_book(token_id)
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
        "spread":       round(best_ask - best_bid, 4),
        "vwap_mid":     round(vwap_mid, 4),
        "num_bids":     len(ob.bids or []),
        "num_asks":     len(ob.asks or []),
        "top_bids":     [(round(float(b.price), 4), round(float(b.size), 2)) for b in bids[:8]],
        "top_asks":     [(round(float(a.price), 4), round(float(a.size), 2)) for a in asks[:8]],
    }, None


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
        "label": label, "color": color, "confidence": conf,
        "obi_now": round(obi_now, 4), "obi_avg": round(avg_obi, 4),
        "combined": combined, "threshold": threshold,
    }
