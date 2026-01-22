import asyncio
import logging
import time
from typing import Dict, List, Optional, Any
from datetime import datetime
from ..base import AbstractExchange
from ..models import (
    Ticker, Order, OrderType, OrderSide, OrderStatus, 
    Balance, Position, FundingRate, MarketInfo
)
from ..errors import ExchangeError

# Import Variational SDK
try:
    from shared_crypto_lib.variational_sdk.variational import VariationalExchange as SDKVariational
except ImportError:
    SDKVariational = None

logger = logging.getLogger("SharedLib.Variational")

class VariationalExchange(AbstractExchange):
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.wallet = config.get('wallet_address')
        self.token = config.get('vr_token')
        self.client = None
        self._asset_info_cache = {}
        self._asset_info_last_update = 0
        self._cache_ttl = 60 # 1 minute

    @staticmethod
    def _parse_max_leverage(data: Optional[Dict[str, Any]]) -> Optional[float]:
        if not isinstance(data, dict):
            return None
        candidates = [
            "max_leverage",
            "maxLeverage",
            "max_lev",
            "max_leverage_multiplier",
            "maxLeverageMultiplier",
        ]
        for key in candidates:
            val = data.get(key)
            if val is None:
                continue
            try:
                parsed = float(val)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        margin_keys = [
            "min_initial_margin_fraction",
            "initial_margin_fraction",
            "min_initial_margin",
            "initial_margin",
        ]
        for key in margin_keys:
            val = data.get(key)
            if val is None:
                continue
            try:
                frac = float(val)
            except (TypeError, ValueError):
                continue
            if frac > 0:
                return 1.0 / frac
        return None
        
    async def initialize(self):
        if not SDKVariational:
            raise ExchangeError("Variational SDK not found.")
            
        options = {}
        slippage = self.config.get("slippage") if self.config else None
        if slippage is not None:
            options["max_slippage"] = slippage

        self.client = SDKVariational(
            evm_wallet_address=self.wallet,
            session_cookies={"vr_token": self.token} if self.token else None,
            evm_private_key=self.config.get("private_key") if self.config else None,
            options=options or None
        )
        await self.client.initialize()
        await self.load_markets() 
        self.is_initialized = True
        logger.info("Variational Initialized.")

    async def _update_asset_info_cache(self):
        """Fetch supported_assets and update cache"""
        now = time.time()
        if now - self._asset_info_last_update < self._cache_ttl and self._asset_info_cache:
            return

        try:
            raw_data = await self.client._supported_assets_raw()
            if isinstance(raw_data, dict):
                self._asset_info_cache = raw_data
                self._asset_info_last_update = now
        except Exception as e:
            logger.error(f"Failed to update asset info cache: {e}")

    async def load_markets(self) -> Dict[str, MarketInfo]:
        """Populate markets using supported_assets_raw"""
        try:
            await self._update_asset_info_cache()
            self.markets = {}
            
            for symbol, data_list in self._asset_info_cache.items():
                # data_list is usually list of dicts. We take first one.
                if not isinstance(data_list, list) or len(data_list) == 0:
                     continue
                
                data = data_list[0]
                # Symbol is e.g. "ETH"
                
                # Create MarketInfo
                m_info = MarketInfo(
                    symbol=symbol,
                    base=symbol,
                    quote='USDC', # Variational uses USDC usually? Or virtual.
                    min_qty=0.1, # Default
                    min_notional=10.0,
                    qty_step=0.1,
                    price_tick=0.01
                )
                m_info.raw = data
                interval_val = data.get("funding_interval_s")
                if interval_val is not None:
                    try:
                        m_info.funding_interval_s = int(interval_val)
                    except (TypeError, ValueError):
                        pass
                max_lev = self._parse_max_leverage(data)
                if max_lev:
                    m_info.max_leverage = max_lev
                
                self.markets[symbol] = m_info
                
            logger.info(f"Variational Markets Loaded: {len(self.markets)}")
            return self.markets
        except Exception as e:
            logger.error(f"VAR Load Markets Failed: {e}")
            return {}

    async def refresh_market_info(self, symbol: str, probe_qty: float = 0.1) -> Optional[MarketInfo]:
        """Update MarketInfo for a symbol using indicative quote qty limits."""
        if not self.client:
            return None
        try:
            core = await self.client._fetch_indicative_quote(symbol, probe_qty)
            ql = core.get("qty_limits", {}) if isinstance(core, dict) else {}
            bid = ql.get("bid", {}) if isinstance(ql, dict) else {}
            ask = ql.get("ask", {}) if isinstance(ql, dict) else {}

            min_qty = bid.get("min_qty") or ask.get("min_qty")
            qty_step = bid.get("min_qty_tick") or ask.get("min_qty_tick")
            max_qty = bid.get("max_qty") or ask.get("max_qty")
            mark_price = core.get("mark_price") or core.get("index_price")

            m = self.markets.get(symbol)
            if not m:
                m = MarketInfo(
                    symbol=symbol,
                    base=symbol,
                    quote="USDC",
                    min_qty=0.0,
                )
                self.markets[symbol] = m

            if min_qty is not None:
                m.min_qty = float(min_qty)
            if qty_step is not None:
                m.qty_step = float(qty_step)
            if max_qty is not None:
                try:
                    m.max_qty = float(max_qty)
                except (TypeError, ValueError):
                    pass
            if mark_price and m.min_notional:
                # keep existing min_notional if set
                pass
            elif mark_price and m.min_qty:
                try:
                    m.min_notional = float(mark_price) * float(m.min_qty)
                except (TypeError, ValueError):
                    pass
            max_lev = self._parse_max_leverage(core if isinstance(core, dict) else None)
            if max_lev:
                m.max_leverage = max_lev

            return m
        except Exception as e:
            logger.warning(f"VAR refresh_market_info failed for {symbol}: {e}")
            return None

    async def subscribe_ticker(self, symbol: str, callback):
        # Variational SDK currently doesn't support easy WS subscription for Ticker in this context
        # We rely on polling via fetch_ticker/funding_rate in MonitoringService
        pass

    async def create_order(self, symbol, type, side, amount, price=None, params={}) -> Order:
        try:
            order_type_str = type.value if isinstance(type, OrderType) else type
            side_str = side.value if isinstance(side, OrderSide) else side
            
            rfq_id = await self.client.create_order(
                symbol=symbol,
                side=side_str,
                amount=amount, 
                price=price,
                order_type=order_type_str
            )
            
            # SDK returns rfq_id string
            # Check if it returned an object/dict in some versions?
            oid = str(rfq_id)
            if hasattr(rfq_id, 'get'):
                 oid = rfq_id.get('order_id', str(rfq_id))

            if not oid or str(oid).lower() in ("0x00", "unknown", "none"):
                await asyncio.sleep(0.2)
                verified = await self._verify_order_created(symbol, oid)
                if verified:
                    oid = verified.id
                else:
                    logger.warning(f"Variational create_order verification failed for {symbol}.")

            return Order(
                id=oid,
                symbol=symbol,
                type=type,
                side=side,
                amount=amount,
                price=price or 0.0,
                status=OrderStatus.OPEN,
                raw=rfq_id
            )
        except Exception as e:
            logger.error(f"Variational Order Failed: {e}")
            raise ExchangeError(f"Variational Order Failed: {e}")

    async def fetch_positions(self) -> List[Position]:
        try:
            raw = await self.client._fetch_positions_all()
            positions = []
            items = []
            if isinstance(raw, dict):
                if isinstance(raw.get("result"), list):
                    items = raw.get("result")
                elif isinstance(raw.get("positions"), list):
                    items = raw.get("positions")
                else:
                    for sym, data in raw.items():
                        if isinstance(data, dict):
                            data = dict(data)
                            data.setdefault("symbol", sym)
                            items.append(data)
            elif isinstance(raw, list):
                items = raw
            elif raw:
                items = [raw]

            def _to_float(val):
                try:
                    return float(val)
                except Exception:
                    return None

            for item in items:
                if not isinstance(item, dict):
                    continue
                info = item.get("position_info") or item
                inst = info.get("instrument") or item.get("instrument") or {}
                symbol = (
                    inst.get("underlying")
                    or info.get("symbol")
                    or item.get("symbol")
                    or item.get("asset")
                    or item.get("coin")
                )
                qty = (
                    _to_float(info.get("qty"))
                    or _to_float(info.get("position_size"))
                    or _to_float(item.get("qty"))
                    or _to_float(item.get("position_size"))
                    or _to_float(item.get("position"))
                    or 0.0
                )
                if qty == 0:
                    continue

                side_raw = str(info.get("side") or item.get("side") or "").lower()
                if side_raw in ("sell", "short"):
                    side = OrderSide.SELL
                elif side_raw in ("buy", "long"):
                    side = OrderSide.BUY
                else:
                    side = OrderSide.BUY if qty > 0 else OrderSide.SELL

                entry_price = _to_float(info.get("avg_entry_price")) or _to_float(info.get("entry_price")) or _to_float(item.get("entry_price")) or 0.0
                unrealized_pnl = _to_float(item.get("upnl")) or _to_float(item.get("unrealized_pnl")) or _to_float(info.get("unrealized_pnl")) or 0.0
                leverage = _to_float(info.get("leverage")) or _to_float(item.get("leverage")) or 1.0
                liquidation_price = _to_float(info.get("liquidation_price")) or _to_float(item.get("liquidation_price")) or 0.0

                positions.append(Position(
                    symbol=str(symbol) if symbol else "Unknown",
                    side=side,
                    amount=abs(qty),
                    entry_price=float(entry_price),
                    unrealized_pnl=float(unrealized_pnl),
                    leverage=float(leverage),
                    liquidation_price=float(liquidation_price),
                ))
            return positions
        except Exception as e:
            logger.error(f"Fetch Position Failed: {e}")
            return []

    async def fetch_open_orders(self, symbol=None) -> List[Order]:
        try:
            raw = await self.client.fetch_open_orders(symbol or "all")
            orders = []
            for r in raw:
                rfq_id = r.get("rfq_id") or r.get("id")
                order_id = r.get("order_id") or r.get("id")
                orders.append(Order(
                    id=str(rfq_id) if rfq_id is not None else str(order_id),
                    symbol=r.get('coin') or r.get('asset') or symbol,
                    type=OrderType.LIMIT,
                    side=OrderSide(r.get('side', 'buy').lower()),
                    amount=float(r.get('qty', 0)),
                    price=float(r.get('price', 0)),
                    status=OrderStatus.OPEN,
                    client_order_id=str(order_id) if order_id and str(order_id) != str(rfq_id) else None,
                    raw=r
                ))
            return orders
        except Exception:
            return []

    async def _verify_order_created(self, symbol: str, order_id: Optional[str]) -> Optional[Order]:
        try:
            open_orders = await self.fetch_open_orders(symbol)
        except Exception as e:
            logger.warning(f"VAR fetch_open_orders verify failed: {e}")
            return None
        for o in open_orders:
            if order_id and str(o.id) == str(order_id):
                return o
        return None

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            await self.client._set_leverage_raw(symbol, leverage)
            try:
                confirm = await self.get_leverage(symbol)
                reported = None
                if isinstance(confirm, dict):
                    if "leverage" in confirm:
                        reported = confirm.get("leverage")
                    elif isinstance(confirm.get("result"), dict) and "leverage" in confirm["result"]:
                        reported = confirm["result"].get("leverage")
                if reported is not None:
                    try:
                        reported_val = float(reported)
                    except (TypeError, ValueError):
                        reported_val = None
                    if reported_val is not None and int(reported_val) != int(leverage):
                        logger.warning(
                            f"VAR leverage mismatch for {symbol}: expected={leverage}, reported={reported_val}"
                        )
            except Exception as e:
                logger.warning(f"VAR leverage verify failed for {symbol}: {e}")
            return True
        except Exception as e:
            logger.error(f"Set Leverage Failed: {e}")
            return False

    async def get_leverage(self, symbol: str) -> Optional[dict]:
        try:
            return await self.client._get_leverage_raw(symbol)
        except Exception as e:
            logger.error(f"Get Leverage Failed: {e}")
            return None

    async def close(self): pass

    async def fetch_ticker(self, symbol: str) -> Ticker:
        try:
            p = await self.client.fetch_price(symbol)
            return Ticker(symbol, p, p, p, 0)
        except Exception as e:
            # logger.warning(f"VAR Fetch Ticker Failed {symbol}: {e}")
            return Ticker(symbol, 0, 0, 0, 0)

    async def fetch_balance(self) -> Balance:
        try:
            collateral = await self.client.get_collateral()
            # Returns float
            # Check if collateral is dict or float
            val = 0.0
            if isinstance(collateral, dict):
                 val = float(
                     collateral.get('total_collateral')
                     or collateral.get('balance')
                     or collateral.get('collateral')
                     or 0
                 )
            else:
                 val = float(collateral)

            free_val = val
            if isinstance(collateral, dict):
                free_val = float(
                    collateral.get('available_collateral')
                    or collateral.get('max_withdrawable_amount')
                    or val
                )

            return Balance(
                currency='USDC',
                total=val,
                free=free_val,
                used=max(0.0, val - free_val)
            )
        except Exception as e:
            logger.error(f"VAR Fetch Balance Failed: {e}")
            return Balance(currency='USDC', total=0.0, free=0.0, used=0.0)

    async def cancel_order(self, id: str, symbol: str = None) -> bool:
        if not self.client:
            logger.error("VAR cancel_order requires initialized client.")
            return False
        try:
            # Use SDK helper to cancel specific RFQ id
            if not symbol:
                open_orders = await self.fetch_open_orders("all")
                target = None
                for o in open_orders:
                    if o.id == str(id) or (o.client_order_id and o.client_order_id == str(id)):
                        target = {"rfq_id": o.id}
                        symbol = o.symbol
                        break
                if not target:
                    return False
                results = await self.client.cancel_orders(symbol, open_orders=[target])
            else:
                results = await self.client.cancel_orders(symbol, open_orders=[{"rfq_id": id}])
            if isinstance(results, list):
                return any(r.get("id") == id and r.get("status") == "Success" for r in results)
            return False
        except Exception as e:
            logger.error(f"VAR cancel_order failed: {e}")
            return False

    async def fetch_order(self, id: str, symbol: str = None) -> Order:
        # Variational SDK has limited order history. Treat non-open as closed.
        if not symbol:
            raise ExchangeError("fetch_order requires symbol for Variational.")
        open_orders = await self.fetch_open_orders(symbol)
        for o in open_orders:
            if o.id == str(id):
                return o
        return Order(
            id=str(id),
            symbol=symbol,
            type=OrderType.LIMIT,
            side=OrderSide.BUY,
            amount=0.0,
            price=0.0,
            status=OrderStatus.CLOSED,
            raw={"note": "not in open orders"},
        )

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        try:
            await self._update_asset_info_cache()
            
            asset_list = self._asset_info_cache.get(symbol.upper())
            if asset_list and len(asset_list) > 0:
                data = asset_list[0]
                
                # "0.1095" -> 0.1095 (APR)
                fr_str = data.get("funding_rate", "0")
                rate = float(fr_str)
                
                # "2026-01-11T08:00:00Z"
                ft_str = data.get("funding_time")
                next_time_ms = 0
                if ft_str:
                     ft_str = ft_str.replace("Z", "+00:00")
                     dt = datetime.fromisoformat(ft_str)
                     next_time_ms = int(dt.timestamp() * 1000)
                     
                return FundingRate(
                    symbol=symbol,
                    rate=rate,
                    next_funding_time=next_time_ms
                )
            return FundingRate(symbol, 0, 0)
        except Exception:
            return FundingRate(symbol, 0, 0)

    async def subscribe_order_book(self, symbol: str, callback): pass
    async def fetch_order_book(self, symbol: str): return None
