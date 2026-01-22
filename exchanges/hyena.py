import logging
import os
import sys
import time
from typing import Dict, List, Optional, Any

from ..base import AbstractExchange
from ..errors import ExchangeError
from ..models import (
    Balance,
    FundingRate,
    MarketInfo,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Ticker,
)

sdk_path = os.getenv("HYPERLIQUID_SDK_PATH")
if sdk_path and sdk_path not in sys.path:
    sys.path.insert(0, sdk_path)

try:
    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange as HLExchange
    from hyperliquid.utils import constants as hl_constants
    from eth_account import Account
except ImportError:
    Info = None
    HLExchange = None
    hl_constants = None
    Account = None

logger = logging.getLogger("SharedLib.Hyena")


class HyenaExchange(AbstractExchange):
    """
    HyENA Exchange adapter (Based/Hyperliquid SDK).
    Requires builder code for HyENA tracking.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        cfg = config or {}
        self.private_key = cfg.get("private_key")
        self.wallet_address = cfg.get("wallet_address")
        self.main_address = (
            cfg.get("main_address")
            or os.getenv("HYPERLIQUID_MAIN_ADDRESS")
            or os.getenv("HYPERLIQUID_API_WALLET_ADDRESS")
        )
        self.dex_id = (cfg.get("dex_id") or "hyna").lower()
        self.use_symbol_prefix = bool(cfg.get("use_symbol_prefix", True))
        self.builder_address = cfg.get(
            "builder_address", "0x1924b8561eeF20e70Ede628A296175D358BE80e5"
        )
        self.builder_fee = int(cfg.get("builder_fee", 0))
        self.approve_builder_fee = bool(cfg.get("approve_builder_fee", False))
        self.builder_max_fee_rate = str(cfg.get("builder_max_fee_rate", "0"))
        self.slippage = float(cfg.get("slippage", 0.05))
        self.base_url = cfg.get("base_url")

        self.info: Optional[Info] = None
        self.exchange: Optional[HLExchange] = None
        self._account = None
        self._markets_loaded_at = 0.0

    @staticmethod
    def _is_valid_address(addr: Optional[str]) -> bool:
        if not addr:
            return False
        addr = addr.strip()
        return addr.startswith("0x") and len(addr) == 42

    def _state_address(self) -> str:
        if self._is_valid_address(self.main_address):
            return self.main_address
        if self._is_valid_address(self.wallet_address):
            return self.wallet_address
        return ""

    def _ensure_symbol(self, symbol: str) -> str:
        sym = symbol or ""
        if not self.use_symbol_prefix:
            return sym
        if ":" in sym:
            return sym
        return f"{self.dex_id}:{sym}"

    def _strip_prefix(self, symbol: str) -> str:
        if ":" in symbol:
            return symbol.split(":", 1)[1]
        return symbol

    def _next_funding_ms(self) -> int:
        now = int(time.time())
        next_hour = (now // 3600 + 1) * 3600
        return next_hour * 1000

    async def initialize(self):
        if not Info or not HLExchange or not Account:
            raise ExchangeError("Hyperliquid SDK not found.")
        if not self.private_key:
            raise ExchangeError("Hyena private_key missing.")

        self._account = Account.from_key(self.private_key)
        if not self.wallet_address:
            self.wallet_address = self._account.address
        if not self.main_address or not self._is_valid_address(self.main_address):
            self.main_address = self.wallet_address

        base_url = self.base_url or hl_constants.MAINNET_API_URL
        self.info = Info(base_url, skip_ws=True, perp_dexs=[self.dex_id])
        self.exchange = HLExchange(
            self._account,
            base_url,
            perp_dexs=[self.dex_id],
        )

        if self.approve_builder_fee:
            try:
                self.exchange.approve_builder_fee(
                    self.builder_address, self.builder_max_fee_rate
                )
            except Exception as e:
                logger.warning(f"Hyena approve_builder_fee failed: {e}")

        await self.load_markets()
        self.is_initialized = True
        logger.info("Hyena Initialized.")

    async def close(self):
        self.is_initialized = False

    async def load_markets(self) -> Dict[str, MarketInfo]:
        if not self.info:
            return {}
        try:
            meta = self.info.meta(dex=self.dex_id)
            markets: Dict[str, MarketInfo] = {}
            for asset in meta.get("universe", []):
                symbol = asset.get("name", "")
                if not symbol:
                    continue
                base = self._strip_prefix(symbol)
                sz_decimals = int(asset.get("szDecimals", 2))
                min_qty = 10 ** (-sz_decimals)
                m = MarketInfo(
                    symbol=symbol,
                    base=base,
                    quote="USDC",
                    min_qty=min_qty,
                    qty_step=min_qty,
                    price_tick=0.0,
                )
                if "maxLeverage" in asset:
                    try:
                        m.max_leverage = float(asset.get("maxLeverage"))
                    except (TypeError, ValueError):
                        pass
                m.raw = asset
                markets[symbol] = m
            self.markets = markets
            self._markets_loaded_at = time.time()
            logger.info(f"Hyena Markets Loaded: {len(self.markets)}")
            return self.markets
        except Exception as e:
            logger.error(f"Hyena load_markets failed: {e}")
            return {}

    async def fetch_ticker(self, symbol: str) -> Ticker:
        if not self.info:
            raise ExchangeError("Hyena not initialized.")
        sym = self._ensure_symbol(symbol)
        mids = self.info.all_mids(dex=self.dex_id)
        price = float(mids.get(sym, 0.0))
        ts = int(time.time() * 1000)
        return Ticker(symbol=sym, bid=price, ask=price, last=price, timestamp=ts)

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        sym = self._ensure_symbol(symbol)
        rate = 0.0
        try:
            if self.info:
                data = self.info.post(
                    "/info", {"type": "metaAndAssetCtxs", "dex": self.dex_id}
                )
                if isinstance(data, list) and len(data) >= 2:
                    meta = data[0]
                    ctxs = data[1]
                    universe = meta.get("universe", [])
                    for idx, asset in enumerate(universe):
                        if asset.get("name") == sym and idx < len(ctxs):
                            rate = float(ctxs[idx].get("funding", 0.0))
                            break
        except Exception as e:
            logger.warning(f"Hyena funding_rate fetch failed: {e}")
        return FundingRate(
            symbol=sym,
            rate=rate,
            predicted_rate=rate,
            next_funding_time=self._next_funding_ms(),
        )

    async def subscribe_ticker(self, symbol: str, callback):
        # Hyena WS not wired in this adapter; use polling.
        pass

    async def fetch_balance(self) -> Dict[str, Balance]:
        address = self._state_address()
        if not self.info or not address:
            raise ExchangeError("Hyena not initialized.")
        state = self.info.user_state(address, dex=self.dex_id)
        margin = state.get("marginSummary", {})
        total = float(margin.get("accountValue", 0.0))
        free = float(margin.get("withdrawable", 0.0))
        used = max(total - free, 0.0)
        return {"USDe": Balance(currency="USDe", total=total, free=free, used=used)}

    async def fetch_positions(self) -> List[Position]:
        address = self._state_address()
        if not self.info or not address:
            raise ExchangeError("Hyena not initialized.")
        state = self.info.user_state(address, dex=self.dex_id)
        positions: List[Position] = []
        for item in state.get("assetPositions", []):
            pos = item.get("position", {})
            coin = pos.get("coin", "")
            szi = float(pos.get("szi", 0.0))
            if szi == 0:
                continue
            side = OrderSide.BUY if szi > 0 else OrderSide.SELL
            leverage = 0.0
            lev = pos.get("leverage") or {}
            if isinstance(lev, dict):
                leverage = float(lev.get("value", 0.0))
            positions.append(
                Position(
                    symbol=coin,
                    side=side,
                    amount=abs(szi),
                    entry_price=float(pos.get("entryPx", 0.0) or 0.0),
                    unrealized_pnl=float(pos.get("unrealizedPnl", 0.0) or 0.0),
                    leverage=leverage,
                    liquidation_price=float(pos.get("liquidationPx", 0.0) or 0.0),
                )
            )
        return positions

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        if not self.exchange:
            raise ExchangeError("Hyena not initialized.")
        sym = self._ensure_symbol(symbol)
        try:
            self.exchange.update_leverage(leverage, sym, is_cross=True)
            return True
        except Exception as e:
            logger.warning(f"Hyena set_leverage failed: {e}")
            return False

    def _apply_qty_step(self, symbol: str, qty: float) -> float:
        if qty <= 0:
            return 0.0
        info = self.markets.get(symbol)
        step = float(info.qty_step) if info else 0.0
        if step > 0:
            return max(step, (qty // step) * step)
        return qty

    def _limit_price(self, symbol: str, is_buy: bool, px: float) -> float:
        if px <= 0:
            return px
        slippage = max(0.0, float(self.slippage))
        px = px * (1 + slippage) if is_buy else px * (1 - slippage)
        return float(f"{px:.5g}")

    async def create_order(
        self,
        symbol: str,
        type: OrderType,
        side: OrderSide,
        amount: float,
        price: Optional[float] = None,
        params: Dict = {},
    ) -> Order:
        if not self.exchange or not self.info:
            raise ExchangeError("Hyena not initialized.")
        sym = self._ensure_symbol(symbol)
        is_buy = side == OrderSide.BUY
        qty = self._apply_qty_step(sym, amount)
        if qty <= 0:
            raise ExchangeError("Hyena order qty below min.")

        order_type = type.value if isinstance(type, OrderType) else str(type)
        if order_type == OrderType.MARKET.value:
            res = self.exchange.market_open(
                sym,
                is_buy,
                qty,
                px=None,
                slippage=self.slippage,
                builder={"b": self.builder_address.lower(), "f": self.builder_fee},
            )
        else:
            if price is None:
                raise ExchangeError("Hyena limit order requires price.")
            order_req = {
                "coin": sym,
                "is_buy": is_buy,
                "sz": qty,
                "limit_px": float(price),
                "order_type": {"limit": {"tif": "Gtc"}},
                "reduce_only": bool(params.get("reduce_only", False)),
            }

        if order_type != OrderType.MARKET.value:
            res = self.exchange.bulk_orders(
                [order_req],
                builder={"b": self.builder_address.lower(), "f": self.builder_fee},
            )

        status = OrderStatus.OPEN
        order_id = ""
        filled = 0.0
        avg_px = float(price or 0.0)
        if isinstance(res, dict) and res.get("status") == "ok":
            statuses = res.get("response", {}).get("data", {}).get("statuses", [])
            if statuses:
                first = statuses[0]
                if "filled" in first:
                    fill = first["filled"]
                    filled = float(fill.get("totalSz", 0.0))
                    avg_px = float(fill.get("avgPx", avg_px))
                    status = OrderStatus.CLOSED
                    order_id = str(fill.get("oid", "")) or order_id
                elif "resting" in first:
                    rest = first["resting"]
                    order_id = str(rest.get("oid", "")) or order_id
                    status = OrderStatus.OPEN
                elif "error" in first:
                    status = OrderStatus.REJECTED
                    err = first.get("error")
                    logger.warning(f"Hyena order rejected: {err}")
        else:
            status = OrderStatus.REJECTED

        return Order(
            id=str(order_id or ""),
            symbol=sym,
            type=OrderType.MARKET if order_type == OrderType.MARKET.value else OrderType.LIMIT,
            side=side,
            amount=qty,
            price=avg_px if avg_px else price,
            status=status,
            filled=filled,
            remaining=max(qty - filled, 0.0),
            raw=res,
        )

    async def cancel_order(self, id: str, symbol: Optional[str] = None) -> bool:
        if not self.exchange:
            raise ExchangeError("Hyena not initialized.")
        if not symbol:
            raise ExchangeError("Hyena cancel_order requires symbol.")
        sym = self._ensure_symbol(symbol)
        try:
            res = self.exchange.cancel(sym, int(id))
            return bool(res and res.get("status") == "ok")
        except Exception as e:
            logger.warning(f"Hyena cancel failed: {e}")
            return False

    async def fetch_order(self, id: str, symbol: Optional[str] = None) -> Order:
        if not self.info or not self.wallet_address:
            raise ExchangeError("Hyena not initialized.")
        sym = self._ensure_symbol(symbol or "")
        open_orders = self.info.open_orders(self.wallet_address, dex=self.dex_id)
        for o in open_orders:
            if str(o.get("oid")) == str(id) and o.get("coin") == sym:
                side = OrderSide.BUY if o.get("side") == "B" else OrderSide.SELL
                return Order(
                    id=str(o.get("oid")),
                    symbol=sym,
                    type=OrderType.LIMIT,
                    side=side,
                    amount=float(o.get("origSz", o.get("sz", 0.0))),
                    price=float(o.get("limitPx", 0.0)),
                    status=OrderStatus.OPEN,
                    raw=o,
                )
        return Order(
            id=str(id),
            symbol=sym,
            type=OrderType.LIMIT,
            side=OrderSide.BUY,
            amount=0.0,
            price=0.0,
            status=OrderStatus.CLOSED,
            raw=None,
        )

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Order]:
        if not self.info or not self.wallet_address:
            raise ExchangeError("Hyena not initialized.")
        sym = self._ensure_symbol(symbol or "") if symbol else None
        orders = []
        for o in self.info.open_orders(self.wallet_address, dex=self.dex_id):
            if sym and o.get("coin") != sym:
                continue
            side = OrderSide.BUY if o.get("side") == "B" else OrderSide.SELL
            orders.append(
                Order(
                    id=str(o.get("oid")),
                    symbol=o.get("coin", ""),
                    type=OrderType.LIMIT,
                    side=side,
                    amount=float(o.get("origSz", o.get("sz", 0.0))),
                    price=float(o.get("limitPx", 0.0)),
                    status=OrderStatus.OPEN,
                    raw=o,
                )
            )
        return orders
