import asyncio
import os
import time
from dataclasses import asdict
from typing import Any, Dict, List

from src.GRVT_Lighter_Bot.config import Config
from src.shared_crypto_lib.exchanges.grvt import GrvtExchange
from src.shared_crypto_lib.exchanges.lighter import LighterExchange
from src.shared_crypto_lib.exchanges.variational import VariationalExchange
from src.shared_crypto_lib.models import OrderSide, OrderType

from ._probe_utils import load_env, now_tag, output_dir, quantize, sleep_until, write_json, write_md, to_plain


def _balance_to_dict(balance: Any) -> Dict[str, Any]:
    if isinstance(balance, dict):
        return {k: asdict(v) if hasattr(v, "__dataclass_fields__") else v for k, v in balance.items()}
    if hasattr(balance, "__dataclass_fields__"):
        return asdict(balance)
    return {"raw": balance}


def _format_ts(ms: int) -> str:
    if not ms:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000))


async def _monitor_ws(exchanges: List[Dict[str, Any]], duration_s: int = 20, interval_s: float = 2.0):
    deadline = time.time() + duration_s
    samples = []

    async def _subscribe(exchange: Dict[str, Any]):
        ex = exchange["ex"]
        exchange["last_ticker"] = {}

        async def _cb(ticker):
            exchange["last_ticker"][ticker.symbol] = ticker

        if exchange["ws_supported"]:
            for sym in exchange["symbols"].values():
                await ex.subscribe_ticker(sym, _cb)

    # Subscribe for WS-capable exchanges
    for exch in exchanges:
        if exch["ws_supported"]:
            await _subscribe(exch)

    # Collect samples
    while time.time() < deadline:
        tick = {"ts": int(time.time())}
        for exch in exchanges:
            ex = exch["ex"]
            name = exch["name"]
            tick[name] = {}
            for base, sym in exch["symbols"].items():
                ticker = exch.get("last_ticker", {}).get(sym)
                if not ticker:
                    ticker = await ex.fetch_ticker(sym)
                funding = await ex.fetch_funding_rate(sym)
                tick[name][base] = {
                    "symbol": sym,
                    "price": getattr(ticker, "last", 0),
                    "bid": getattr(ticker, "bid", 0),
                    "ask": getattr(ticker, "ask", 0),
                    "funding_rate": getattr(funding, "rate", 0),
                    "next_funding_time": getattr(funding, "next_funding_time", 0),
                }
        samples.append(tick)
        await asyncio.sleep(sleep_until(deadline, interval_s))

    return samples


def _get_market_meta(exchange: Dict[str, Any]) -> Dict[str, Any]:
    ex = exchange["ex"]
    meta = {}
    for base, sym in exchange["symbols"].items():
        m = ex.markets.get(sym)
        meta[base] = {
            "symbol": sym,
            "min_qty": getattr(m, "min_qty", None) if m else None,
            "qty_step": getattr(m, "qty_step", None) if m else None,
            "max_leverage": getattr(m, "max_leverage", None) if m else None,
        }
        if exchange["name"] == "VAR":
            # Reach into cache for funding_interval_s if available
            try:
                data_list = ex._asset_info_cache.get(base.upper()) if hasattr(ex, "_asset_info_cache") else None
                if data_list and isinstance(data_list, list) and data_list:
                    meta[base]["funding_interval_s"] = data_list[0].get("funding_interval_s")
            except Exception:
                pass
    return meta


async def _place_limit_order(exchange: Dict[str, Any], symbol: str, qty: float, price: float, leverage: int):
    ex = exchange["ex"]
    await ex.set_leverage(symbol, leverage)
    return await ex.create_order(symbol, OrderType.LIMIT, OrderSide.BUY, qty, price)


async def _place_market_order(exchange: Dict[str, Any], symbol: str, qty: float, leverage: int, price: float = None):
    ex = exchange["ex"]
    await ex.set_leverage(symbol, leverage)
    return await ex.create_order(symbol, OrderType.MARKET, OrderSide.BUY, qty, price)


async def _close_position(exchange: Dict[str, Any], symbol: str, qty: float):
    ex = exchange["ex"]
    return await ex.create_order(symbol, OrderType.MARKET, OrderSide.SELL, qty)

def _match_order(o, order_id: str, qty: float, price: float, qty_tol: float, price_tol: float) -> bool:
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

async def _wait_for_positions(ex, symbol: str, timeout_s: int = 30, interval_s: float = 2.0):
    deadline = time.time() + timeout_s
    last_positions = []
    while time.time() < deadline:
        try:
            positions = await ex.fetch_positions()
        except Exception:
            positions = []
        last_positions = [p for p in positions if p.symbol == symbol]
        if last_positions:
            return last_positions
        await asyncio.sleep(interval_s)
    return last_positions

async def _wait_for_no_positions(ex, symbol: str, timeout_s: int = 30, interval_s: float = 2.0):
    deadline = time.time() + timeout_s
    last_positions = []
    while time.time() < deadline:
        try:
            positions = await ex.fetch_positions()
        except Exception:
            positions = []
        last_positions = [p for p in positions if p.symbol == symbol]
        if not last_positions:
            return last_positions
        await asyncio.sleep(interval_s)
    return last_positions


async def main():
    env_path = load_env()
    allow_live = os.getenv("ALLOW_LIVE_TRADING", "0") == "1"
    max_step = int(os.getenv("SCENARIO_MAX_STEP", "8"))
    var_trade_symbol = os.getenv("VAR_TRADE_SYMBOL", "ETH").upper()

    report = {
        "env_path": str(env_path),
        "allow_live_trading": allow_live,
        "max_step": max_step,
        "started_at": time.time(),
        "steps": {},
    }

    exchanges = []
    # GRVT
    grvt = GrvtExchange(
        config={
            "api_key": Config.GRVT_API_KEY,
            "private_key": Config.GRVT_PRIVATE_KEY,
            "subaccount_id": Config.GRVT_TRADING_ACCOUNT_ID,
            "env": Config.GRVT_ENV,
        }
    )
    # Lighter
    lighter = LighterExchange(
        config={
            "wallet_address": Config.LIGHTER_WALLET_ADDRESS,
            "private_key": Config.LIGHTER_PRIVATE_KEY,
            "public_key": Config.LIGHTER_PUBLIC_KEY,
            "api_key_index": Config.LIGHTER_API_KEY_INDEX,
            "env": Config.LIGHTER_ENV,
        }
    )
    # Variational
    variational = VariationalExchange(
        config={
            "wallet_address": Config.VARIATIONAL_WALLET_ADDRESS,
            "vr_token": Config.VARIATIONAL_VR_TOKEN,
            "private_key": Config.VARIATIONAL_PRIVATE_KEY,
            "slippage": Config.VARIATIONAL_SLIPPAGE,
        }
    )

    exchanges.append(
        {
            "name": "GRVT",
            "ex": grvt,
            "symbols": {"BTC": "BTC_USDT_Perp", "ETH": "ETH_USDT_Perp"},
            "ws_supported": True,
        }
    )
    exchanges.append(
        {
            "name": "LGHT",
            "ex": lighter,
            "symbols": {"BTC": "BTC-USDT", "ETH": "ETH-USDT"},
            "ws_supported": True,
        }
    )
    exchanges.append(
        {
            "name": "VAR",
            "ex": variational,
            "symbols": {"BTC": "BTC", "ETH": "ETH"},
            "ws_supported": False,
        }
    )

    # Initialize all exchanges
    init_errors = {}
    for exch in exchanges:
        try:
            await exch["ex"].initialize()
        except Exception as e:
            init_errors[exch["name"]] = str(e)
    report["steps"]["init_errors"] = init_errors

    balances = {}
    if max_step >= 1:
        # Step 1: Balances
        for exch in exchanges:
            try:
                bal = await exch["ex"].fetch_balance()
                balances[exch["name"]] = _balance_to_dict(bal)
            except Exception as e:
                balances[exch["name"]] = {"error": str(e)}
        report["steps"]["balances"] = balances
    else:
        report["steps"]["balances"] = "skipped"

    samples = []
    if max_step >= 2:
        # Step 2: WS monitoring (20s, 5-10 samples)
        samples = await _monitor_ws(exchanges, duration_s=20, interval_s=2.0)
        report["steps"]["ws_samples"] = samples
    else:
        report["steps"]["ws_samples"] = "skipped"

    meta = {}
    funding = {}
    if max_step >= 3:
        # Step 3: Funding meta + market info
        for exch in exchanges:
            if exch["name"] == "VAR" and hasattr(exch["ex"], "refresh_market_info"):
                for base in exch["symbols"].keys():
                    try:
                        await exch["ex"].refresh_market_info(base)
                    except Exception:
                        pass
            meta[exch["name"]] = _get_market_meta(exch)
            funding[exch["name"]] = {}
            for base, sym in exch["symbols"].items():
                try:
                    fr = await exch["ex"].fetch_funding_rate(sym)
                    funding[exch["name"]][base] = asdict(fr)
                except Exception as e:
                    funding[exch["name"]][base] = {"error": str(e)}
        report["steps"]["market_meta"] = meta
        report["steps"]["funding_info"] = funding
    else:
        report["steps"]["market_meta"] = "skipped"
        report["steps"]["funding_info"] = "skipped"

    # Steps 4-8: Trading scenario on ETH only
    trade_results = {}
    if max_step < 4:
        trade_results["skipped"] = f"SCENARIO_MAX_STEP={max_step} (steps 4-8 skipped)."
    elif allow_live:
        for exch in exchanges:
            name = exch["name"]
            sym = exch["symbols"]["ETH"]
            if name == "VAR":
                sym = var_trade_symbol
            trade_results[name] = {"symbol": sym}
            ex = exch["ex"]

            try:
                if name == "VAR" and hasattr(ex, "refresh_market_info"):
                    refreshed = await ex.refresh_market_info(sym)
                    if refreshed:
                        trade_results[name]["market_info_refreshed"] = to_plain(refreshed)

                ticker = await ex.fetch_ticker(sym)
                last = getattr(ticker, "last", 0) or 0
                bid = getattr(ticker, "bid", 0) or 0
                anchor = bid or last
                if anchor <= 0:
                    trade_results[name]["error"] = "No valid price from ticker; skipping trade flow."
                    continue
                limit_price = max(anchor - 200.0, anchor * 0.9)

                market_info = ex.markets.get(sym)
                min_qty = getattr(market_info, "min_qty", 0.0) if market_info else 0.0
                step = getattr(market_info, "qty_step", 0.0) if market_info else 0.0
                qty = max(0.01, min_qty or 0.0)
                qty = quantize(qty, step)
                # Ensure minimum notional (fallback to $10 if exchange does not provide)
                min_notional = getattr(market_info, "min_notional", None) if market_info else None
                if not min_notional or min_notional <= 0:
                    min_notional = 10.0
                if limit_price * qty < min_notional:
                    qty = quantize(min_notional / limit_price, step)

                trade_results[name]["limit"] = {
                    "qty": qty,
                    "price": limit_price,
                    "leverage": 10,
                }

                limit_order = await _place_limit_order(exch, sym, qty, limit_price, leverage=10)
                limit_id = getattr(limit_order, "client_order_id", None) or getattr(limit_order, "id", None)
                if limit_id in ("0x00", "unknown"):
                    limit_id = None
                trade_results[name]["limit_order_id"] = limit_id
                trade_results[name]["limit_order_raw"] = to_plain(getattr(limit_order, "raw", None))

                predicted_ms = getattr(limit_order, "timestamp", None)
                if predicted_ms:
                    wait_s = max(5, (predicted_ms - int(time.time() * 1000)) / 1000 + 2)
                    await asyncio.sleep(wait_s)
                else:
                    await asyncio.sleep(6 if exch["name"] == "LGHT" else 3)
                open_orders = []
                qty_tol = max(1e-9, getattr(market_info, "qty_step", 0.0) or 0.0)
                price_tol = max(1e-6, getattr(market_info, "price_tick", 0.0) or 1.0)
                if limit_id:
                    open_orders = await ex.fetch_open_orders(sym)
                    is_open = any(_match_order(o, limit_id, qty, limit_price, qty_tol, price_tol) for o in open_orders)
                    trade_results[name]["limit_open"] = is_open
                elif exch["name"] in ("GRVT", "LGHT"):
                    open_orders = await ex.fetch_open_orders(sym)
                    for o in open_orders:
                        if _match_order(o, limit_id, qty, limit_price, qty_tol, price_tol):
                            limit_id = o.id
                            trade_results[name]["limit_order_id"] = limit_id
                            trade_results[name]["limit_open"] = True
                            break
                if "limit_open" not in trade_results[name]:
                    trade_results[name]["limit_open"] = False

                # Cancel limit order
                if limit_id:
                    cancel_ok = await ex.cancel_order(limit_id, sym)
                    if not cancel_ok and exch["name"] == "LGHT" and hasattr(ex, "client"):
                        try:
                            tx, resp, err = await ex.client.cancel_all_orders(
                                time_in_force=ex.client.CANCEL_ALL_TIF_IMMEDIATE,
                                timestamp_ms=0,
                            )
                            trade_results[name]["limit_cancel_fallback"] = to_plain(
                                {"tx": tx, "resp": resp, "err": err}
                            )
                            cancel_ok = err is None
                        except Exception as e:
                            trade_results[name]["limit_cancel_fallback_error"] = str(e)
                    trade_results[name]["limit_cancelled"] = cancel_ok
                else:
                    trade_results[name]["limit_cancelled"] = False

                # Market entry (5x)
                market_price = None
                if exch["name"] == "LGHT":
                    market_price = None
                market_order = await _place_market_order(exch, sym, qty, leverage=5, price=market_price)
                market_id = getattr(market_order, "id", None)
                if market_id == "0x00":
                    market_id = None
                trade_results[name]["market_order_id"] = market_id

                predicted_ms = getattr(market_order, "timestamp", None)
                if predicted_ms:
                    wait_s = max(3, (predicted_ms - int(time.time() * 1000)) / 1000 + 2)
                    await asyncio.sleep(wait_s)
                pos_wait = 60 if exch["name"] == "VAR" else 30
                positions = await _wait_for_positions(ex, sym, timeout_s=pos_wait, interval_s=2)
                trade_results[name]["positions"] = [to_plain(p) for p in positions]
                try:
                    if market_id:
                        trade_results[name]["market_order_status"] = to_plain(
                            await ex.fetch_order(market_id, sym)
                        )
                except Exception as e:
                    trade_results[name]["market_order_status_error"] = str(e)
                if not positions and exch["name"] == "LGHT":
                    try:
                        from lighter.api import account_api
                        if ex.client and ex.client.api_client:
                            api_client = ex.client.api_client
                        else:
                            from lighter.api_client import ApiClient
                            api_client = ApiClient(ex.config_obj)
                        if ex.auth_token:
                            api_client.default_headers["Authorization"] = ex.auth_token
                        api = account_api.AccountApi(api_client)
                        import inspect
                        if inspect.iscoroutinefunction(api.account):
                            res = await api.account(by="index", value=str(ex.account_index))
                        else:
                            res = await asyncio.to_thread(api.account, by="index", value=str(ex.account_index))
                        trade_results[name]["positions_raw"] = to_plain(
                            res.to_dict() if hasattr(res, "to_dict") else res
                        )
                    except Exception as e:
                        trade_results[name]["positions_raw_error"] = str(e)
                if not positions and exch["name"] == "VAR":
                    try:
                        if getattr(ex, "client", None):
                            trade_results[name]["positions_raw"] = to_plain(
                                await ex.client._fetch_positions_all()
                            )
                        open_orders_after = await ex.fetch_open_orders(sym)
                        trade_results[name]["open_orders_after_market"] = [to_plain(o) for o in open_orders_after]
                        if open_orders_after:
                            cancel_results = []
                            for o in open_orders_after:
                                try:
                                    ok = await ex.cancel_order(o.id, sym)
                                    cancel_results.append({"id": o.id, "ok": ok})
                                except Exception as ce:
                                    cancel_results.append({"id": o.id, "error": str(ce)})
                            trade_results[name]["cancel_after_market"] = cancel_results
                    except Exception as e:
                        trade_results[name]["positions_raw_error"] = str(e)

                # Close long ETH position if found
                close_ok = False
                for p in positions:
                    if p.symbol == sym and p.side == OrderSide.BUY and p.amount > 0:
                        close_order = await _close_position(exch, sym, p.amount)
                        trade_results[name]["close_order_id"] = close_order.id
                        close_ok = True
                        break
                trade_results[name]["closed"] = close_ok
                if close_ok:
                    positions_after = await _wait_for_no_positions(ex, sym, timeout_s=30, interval_s=2)
                    trade_results[name]["positions_after_close"] = [to_plain(p) for p in positions_after]
            except Exception as e:
                trade_results[name]["error"] = str(e)
    else:
        trade_results["skipped"] = "ALLOW_LIVE_TRADING is not set to 1. Steps 4-8 skipped."
    report["steps"]["trade_flow"] = trade_results

    report["completed_at"] = time.time()

    tag = now_tag()
    out_dir = output_dir()
    json_path = out_dir / f"scenario_full_mainnet_{tag}.json"
    md_path = out_dir / f"scenario_full_mainnet_{tag}.md"

    write_json(json_path, to_plain(report))

    md_lines = []
    md_lines.append("# Exchange Scenario Report")
    md_lines.append("")
    md_lines.append(f"- env_path: `{report['env_path']}`")
    md_lines.append(f"- allow_live_trading: `{report['allow_live_trading']}`")
    md_lines.append("")
    md_lines.append("## Step 1: Balances")
    for k, v in balances.items():
        md_lines.append(f"- {k}: `{v}`")
    md_lines.append("")
    md_lines.append("## Step 2: WS Monitoring (Samples)")
    md_lines.append(f"- samples: `{len(samples)}`")
    md_lines.append("")
    md_lines.append("## Step 3: Funding + Market Meta")
    md_lines.append(f"- funding_info: `{funding}`")
    md_lines.append(f"- market_meta: `{meta}`")
    md_lines.append("")
    md_lines.append("## Steps 4-8: Trading Flow (ETH)")
    md_lines.append(f"- results: `{to_plain(trade_results)}`")
    write_md(md_path, md_lines)

    print(f"Report JSON: {json_path}")
    print(f"Report MD: {md_path}")

    for exch in exchanges:
        try:
            await exch["ex"].close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
