import asyncio
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from src.GRVT_Lighter_Bot.config import Config
from src.shared_crypto_lib.exchanges.grvt import GrvtExchange
from src.shared_crypto_lib.exchanges.lighter import LighterExchange
from src.shared_crypto_lib.models import OrderSide, OrderType

from ._probe_utils import load_env, now_tag, output_dir, quantize, write_json, write_md, to_plain


def _balance_to_dict(balance: Any) -> Dict[str, Any]:
    if isinstance(balance, dict):
        return {k: asdict(v) if hasattr(v, "__dataclass_fields__") else v for k, v in balance.items()}
    if hasattr(balance, "__dataclass_fields__"):
        return asdict(balance)
    return {"raw": balance}


async def _cancel_all_orders_grvt(ex: GrvtExchange, symbol: str) -> Dict[str, Any]:
    result = {"cancelled": [], "errors": []}
    try:
        open_orders = await ex.fetch_open_orders(symbol)
        for o in open_orders:
            try:
                ok = await ex.cancel_order(o.id, symbol)
                result["cancelled"].append({"id": o.id, "ok": ok})
            except Exception as e:
                result["errors"].append(str(e))
    except Exception as e:
        result["errors"].append(str(e))
    return result


async def _cancel_all_orders_lighter(ex: LighterExchange, symbol: str) -> Dict[str, Any]:
    result = {"cancelled": [], "errors": [], "fallback": None}
    try:
        open_orders = await ex.fetch_open_orders(symbol)
        for o in open_orders:
            try:
                ok = await ex.cancel_order(o.id, symbol)
                result["cancelled"].append({"id": o.id, "ok": ok})
            except Exception as e:
                result["errors"].append(str(e))
    except Exception as e:
        result["errors"].append(str(e))
        # Fallback to cancel_all_orders (SDK)
        try:
            tx, resp, err = await ex.client.cancel_all_orders(
                time_in_force=ex.client.CANCEL_ALL_TIF_IMMEDIATE,
                timestamp_ms=0,
            )
            result["fallback"] = {"tx": to_plain(tx), "resp": to_plain(resp), "err": err}
        except Exception as e2:
            result["errors"].append(str(e2))
    return result


async def _place_limit(ex, symbol: str, qty: float, price: float, leverage: int):
    await ex.set_leverage(symbol, leverage)
    return await ex.create_order(symbol, OrderType.LIMIT, OrderSide.BUY, qty, price)


async def _place_market(ex, symbol: str, qty: float, leverage: int, price: Optional[float] = None):
    await ex.set_leverage(symbol, leverage)
    return await ex.create_order(symbol, OrderType.MARKET, OrderSide.BUY, qty, price)


async def _close_market(ex, symbol: str, qty: float):
    return await ex.create_order(symbol, OrderType.MARKET, OrderSide.SELL, qty)

def _match_order(o, order_id: Optional[str], qty: float, price: float, qty_tol: float, price_tol: float) -> bool:
    if order_id:
        if str(o.id) == str(order_id):
            return True
        if getattr(o, "client_order_id", None) and str(o.client_order_id) == str(order_id):
            return True
    if qty is None or price is None:
        return False
    if abs(o.amount - qty) <= qty_tol and abs((o.price or 0) - price) <= price_tol:
        return True
    return False


async def _monitor_open(
    ex,
    symbol: str,
    qty: float,
    price: float,
    order_id: Optional[str] = None,
    qty_tol: float = 1e-9,
    price_tol: float = 1.0,
    timeout_s: int = 20,
    interval_s: float = 2.0,
):
    deadline = time.time() + timeout_s
    last_orders = []
    while time.time() < deadline:
        try:
            last_orders = await ex.fetch_open_orders(symbol)
            for o in last_orders:
                if _match_order(o, order_id, qty, price, qty_tol, price_tol):
                    return True, last_orders
        except Exception:
            pass
        await asyncio.sleep(interval_s)
    return False, last_orders


async def _check_positions(ex, symbol: str):
    try:
        positions = await ex.fetch_positions()
    except Exception:
        positions = []
    return [p for p in positions if p.symbol == symbol]

async def _wait_for_positions(
    ex,
    symbol: str,
    timeout_s: int = 30,
    interval_s: float = 2.0,
) -> List[Any]:
    deadline = time.time() + timeout_s
    last_positions: List[Any] = []
    while time.time() < deadline:
        last_positions = await _check_positions(ex, symbol)
        if last_positions:
            return last_positions
        await asyncio.sleep(interval_s)
    return last_positions

async def _wait_for_no_positions(
    ex,
    symbol: str,
    timeout_s: int = 30,
    interval_s: float = 2.0,
) -> List[Any]:
    deadline = time.time() + timeout_s
    last_positions: List[Any] = []
    while time.time() < deadline:
        last_positions = await _check_positions(ex, symbol)
        if not last_positions:
            return last_positions
        await asyncio.sleep(interval_s)
    return last_positions


def _compare_balances(balance: Dict[str, Any]) -> Dict[str, Any]:
    # Expect total == free after cleanup
    try:
        total = float(balance.get("total", 0))
        free = float(balance.get("free", 0))
        return {"total": total, "free": free, "match": abs(total - free) < 1e-6}
    except Exception:
        return {"raw": balance}


async def run_exchange_flow(name: str, ex, symbol: str, price: float, qty: float) -> Dict[str, Any]:
    result: Dict[str, Any] = {"symbol": symbol}

    # 1) Place limit
    limit_order = await _place_limit(ex, symbol, qty, price, leverage=10)
    result["limit_order"] = to_plain(limit_order)
    limit_id = getattr(limit_order, "client_order_id", None) or getattr(limit_order, "id", None)
    if limit_id in ("0x00", "unknown"):
        limit_id = None

    # 2) Monitor open (respect predicted execution time if provided)
    predicted_ms = getattr(limit_order, "timestamp", None)
    if predicted_ms:
        wait_s = max(5, (predicted_ms - int(time.time() * 1000)) / 1000 + 2)
        await asyncio.sleep(wait_s)
    market_info = getattr(ex, "markets", {}).get(symbol)
    qty_tol = max(1e-9, getattr(market_info, "qty_step", 0.0) or 0.0)
    price_tol = max(1e-6, getattr(market_info, "price_tick", 0.0) or 1.0)
    is_open, open_orders = await _monitor_open(
        ex,
        symbol,
        qty,
        price,
        order_id=limit_id,
        qty_tol=qty_tol,
        price_tol=price_tol,
        timeout_s=30,
        interval_s=2,
    )
    result["limit_open"] = is_open
    result["open_orders_count"] = len(open_orders)

    # 3) Cancel if open
    cancel_ok = False
    if limit_id:
        cancel_ok = await ex.cancel_order(limit_id, symbol)
    elif open_orders:
        cancel_ok = True
        for o in open_orders:
            ok = await ex.cancel_order(o.id, symbol)
            cancel_ok = cancel_ok and ok
    result["limit_cancelled"] = cancel_ok

    await asyncio.sleep(3)
    try:
        result["limit_status"] = to_plain(await ex.fetch_order(limit_id, symbol)) if limit_id else None
    except Exception as e:
        result["limit_status_error"] = str(e)
    try:
        open_orders_after = await ex.fetch_open_orders(symbol)
        result["open_orders_after_cancel"] = len(open_orders_after)
    except Exception as e:
        result["open_orders_after_cancel_error"] = str(e)

    # 4) Market entry
    market_price = None
    market_order = await _place_market(ex, symbol, qty, leverage=5, price=market_price)
    result["market_order"] = to_plain(market_order)

    # 5) Check positions
    predicted_ms = getattr(market_order, "timestamp", None)
    if predicted_ms:
        wait_s = max(3, (predicted_ms - int(time.time() * 1000)) / 1000 + 2)
        await asyncio.sleep(wait_s)
    positions = await _wait_for_positions(ex, symbol, timeout_s=30, interval_s=2)
    result["positions_after_entry"] = [to_plain(p) for p in positions]

    # 6) Close position
    closed = False
    for p in positions:
        if p.side == OrderSide.BUY and p.amount > 0:
            close_order = await _close_market(ex, symbol, p.amount)
            result["close_order"] = to_plain(close_order)
            closed = True
            break
    result["closed"] = closed

    # 7) Verify positions cleared
    positions_after = await _wait_for_no_positions(ex, symbol, timeout_s=30, interval_s=2)
    result["positions_after_close"] = [to_plain(p) for p in positions_after]

    return result


async def main():
    env_path = load_env()

    report: Dict[str, Any] = {"env_path": str(env_path), "started_at": time.time(), "steps": {}}

    grvt = GrvtExchange(
        config={
            "api_key": Config.GRVT_API_KEY,
            "private_key": Config.GRVT_PRIVATE_KEY,
            "subaccount_id": Config.GRVT_TRADING_ACCOUNT_ID,
            "env": Config.GRVT_ENV,
        }
    )
    lighter = LighterExchange(
        config={
            "wallet_address": Config.LIGHTER_WALLET_ADDRESS,
            "private_key": Config.LIGHTER_PRIVATE_KEY,
            "public_key": Config.LIGHTER_PUBLIC_KEY,
            "api_key_index": Config.LIGHTER_API_KEY_INDEX,
            "env": Config.LIGHTER_ENV,
        }
    )

    await grvt.initialize()
    await lighter.initialize()

    grvt_symbol = "ETH_USDT_Perp"
    lght_symbol = "ETH-USDT"

    # 0) Cleanup existing limit orders
    report["steps"]["cancel_grvt"] = await _cancel_all_orders_grvt(grvt, grvt_symbol)
    report["steps"]["cancel_lght"] = await _cancel_all_orders_lighter(lighter, lght_symbol)

    # 1) Balances before
    report["steps"]["balance_before"] = {
        "GRVT": _balance_to_dict(await grvt.fetch_balance()).get("USDT", {}),
        "LGHT": _balance_to_dict(await lighter.fetch_balance()),
    }

    # 2) Determine limit price (200 below)
    grvt_ticker = await grvt.fetch_ticker(grvt_symbol)
    lght_ticker = await lighter.fetch_ticker(lght_symbol)
    grvt_anchor = grvt_ticker.bid or grvt_ticker.last or 0
    lght_anchor = lght_ticker.bid or lght_ticker.last or 0
    if lght_anchor <= 0:
        lght_anchor = grvt_anchor
    grvt_price = max(1.0, (grvt_anchor - 200) if grvt_anchor > 0 else 1.0)
    lght_price = max(1.0, (lght_anchor - 200) if lght_anchor > 0 else 1.0)

    grvt_qty = max(0.01, getattr(grvt.markets.get(grvt_symbol), "min_qty", 0.0) or 0.01)
    lght_qty = max(0.01, getattr(lighter.markets.get(lght_symbol), "min_qty", 0.0) or 0.01)

    grvt_qty = quantize(grvt_qty, getattr(grvt.markets.get(grvt_symbol), "qty_step", 0.0) or 0.0)
    lght_qty = quantize(lght_qty, getattr(lighter.markets.get(lght_symbol), "qty_step", 0.0) or 0.0)

    report["steps"]["trade_flow"] = {}
    try:
        report["steps"]["trade_flow"]["GRVT"] = await run_exchange_flow("GRVT", grvt, grvt_symbol, grvt_price, grvt_qty)
    except Exception as e:
        report["steps"]["trade_flow"]["GRVT"] = {"error": str(e)}
    try:
        report["steps"]["trade_flow"]["LGHT"] = await run_exchange_flow("LGHT", lighter, lght_symbol, lght_price, lght_qty)
    except Exception as e:
        report["steps"]["trade_flow"]["LGHT"] = {"error": str(e)}

    # 2.5) Cleanup leftover orders after trade flow
    report["steps"]["cancel_after"] = {
        "GRVT": await _cancel_all_orders_grvt(grvt, grvt_symbol),
        "LGHT": await _cancel_all_orders_lighter(lighter, lght_symbol),
    }

    # 3) Balances after
    report["steps"]["balance_after"] = {
        "GRVT": _balance_to_dict(await grvt.fetch_balance()).get("USDT", {}),
        "LGHT": _balance_to_dict(await lighter.fetch_balance()),
    }
    report["steps"]["balance_check"] = {
        "GRVT": _compare_balances(report["steps"]["balance_after"]["GRVT"]),
        "LGHT": _compare_balances(report["steps"]["balance_after"]["LGHT"]),
    }

    report["completed_at"] = time.time()

    tag = now_tag()
    out_dir = output_dir()
    json_path = out_dir / f"scenario_grvt_lighter_flow_{tag}.json"
    md_path = out_dir / f"scenario_grvt_lighter_flow_{tag}.md"

    write_json(json_path, to_plain(report))
    md_lines = [
        "# GRVT + Lighter Trade Flow Report",
        "",
        f"- env_path: `{report['env_path']}`",
        "",
        "## Balance Check (After)",
        f"- GRVT: `{report['steps']['balance_check']['GRVT']}`",
        f"- LGHT: `{report['steps']['balance_check']['LGHT']}`",
        "",
        "## Trade Flow Results",
        f"- GRVT: `{report['steps']['trade_flow']['GRVT']}`",
        f"- LGHT: `{report['steps']['trade_flow']['LGHT']}`",
    ]
    write_md(md_path, md_lines)

    print(f"Report JSON: {json_path}")
    print(f"Report MD: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
