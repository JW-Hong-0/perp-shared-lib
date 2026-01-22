import asyncio
import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Any
from ..base import AbstractExchange
from ..models import (
    Ticker, Order, OrderType, OrderSide, OrderStatus, 
    Balance, Position, FundingRate, MarketInfo
)
from ..errors import ExchangeError, NetworkError, OrderNotFound

try:
    from pysdk.grvt_ccxt import GrvtCcxt
    from pysdk.grvt_ccxt_env import GrvtEnv
    from pysdk.grvt_ccxt_utils import get_EIP712_domain_data, rand_uint32
except ImportError:
    GrvtCcxt = None
    GrvtEnv = None
    get_EIP712_domain_data = None
    rand_uint32 = None

try:
    from pysdk.grvt_ccxt_ws import GrvtCcxtWS
except ImportError:
    GrvtCcxtWS = None

logger = logging.getLogger("SharedLib.GRVT")

class GrvtExchange(AbstractExchange):
    """
    GRVT Implementation of AbstractExchange.
    Relies on 'pysdk' library.
    """
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = config.get('api_key')
        self.private_key = config.get('private_key')
        self.subaccount_id = config.get('subaccount_id') # trading_account_id
        self.env_str = config.get('env', 'PROD')
        self.disable_leverage_update = bool(config.get('disable_leverage_update', False))
        self.use_position_config = bool(config.get('use_position_config', False))
        self.position_margin_type = str(config.get('position_margin_type', 'CROSS')).upper()
        self._leverage_deprecated = False
        self._leverage_skip_logged = False
        self._position_config_cache: Dict[str, str] = {}
        self._last_position_config_variant: Optional[str] = None
        self._last_position_config_response: Optional[dict] = None
        self._last_position_config_payload: Optional[dict] = None
        self.client = None
    
    async def initialize(self):
        if not GrvtCcxt:
            raise ExchangeError("pysdk not installed or not found.")
            
        self.env_enum = GrvtEnv.TESTNET if self.env_str == 'TESTNET' else GrvtEnv.PROD
        env = self.env_enum
        
        # Initialize client (Sync constructor, async calls usually)
        # Note: GrvtCcxt might differ in sync/async nature. 
        # Based on ref bot, calls are run in thread for fetch_ticker etc?
        # "orders = await asyncio.to_thread(self.client.fetch_open_orders, grvt_symbol)"
        # So GrvtCcxt is synchronous.
        
        self.client = GrvtCcxt(
            env=env,
            logger=logger,
            parameters={
                "api_key": self.api_key,
                "private_key": self.private_key,
                "trading_account_id": self.subaccount_id,
            }
        )
        
        # Load Markets sync
        # await asyncio.to_thread(self.client.load_markets) # Called inside self.load_markets if needed
        await self.load_markets()
        self.is_initialized = True
        logger.info("GRVT Initialized (Standardized).")

    async def close(self):
        # Close WS client and aiohttp sessions if created
        try:
            if hasattr(self, "ws_client") and self.ws_client:
                try:
                    await self.ws_client.__aexit__()
                except Exception:
                    pass
                try:
                    if getattr(self.ws_client, "_session", None):
                        await self.ws_client._session.close()
                except Exception:
                    pass
                self.ws_client = None
        except Exception:
            pass
        # Close requests session in GrvtCcxt
        try:
            if self.client and getattr(self.client, "_session", None):
                self.client._session.close()
        except Exception:
            pass
        
    async def load_markets(self) -> Dict[str, MarketInfo]:
        # self.client.markets is available after load_markets()
        if not self.client.markets:
             await asyncio.to_thread(self.client.load_markets)

        leverage_limits: Dict[str, float] = {}
        try:
            from pysdk.grvt_raw_sync import GrvtRawSync
            from pysdk.grvt_raw_base import GrvtApiConfig
            from pysdk import grvt_raw_types as raw_types
            from pysdk.grvt_raw_env import GrvtEnv as RawEnv

            raw_env = RawEnv.TESTNET if self.env_str == 'TESTNET' else RawEnv.PROD
            cfg = GrvtApiConfig(
                env=raw_env,
                trading_account_id=self.subaccount_id,
                private_key=self.private_key,
                api_key=self.api_key,
                logger=logger,
            )
            raw = GrvtRawSync(cfg)
            resp = raw.get_all_initial_leverage_v1(
                raw_types.ApiGetAllInitialLeverageRequest(
                    sub_account_id=str(self.subaccount_id)
                )
            )
            if hasattr(resp, "results"):
                for item in resp.results:
                    try:
                        leverage_limits[item.instrument] = float(item.max_leverage)
                    except (TypeError, ValueError):
                        continue
        except Exception as e:
            logger.warning(f"GRVT leverage limits fetch failed: {e}")
             
        for symbol, info in self.client.markets.items():
            # info contains GRVT instrument metadata (tick_size/min_size/base_decimals/etc).
            limits = info.get('limits', {})
            amount_limits = limits.get('amount', {})

            min_qty = float(info.get('min_size') or amount_limits.get('min') or 0.0)
            max_qty = info.get('max_position_size')
            try:
                max_qty = float(max_qty) if max_qty is not None else None
            except (TypeError, ValueError):
                max_qty = None

            tick_size = info.get('tick_size') or info.get('precision', {}).get('price')
            try:
                price_tick = float(tick_size or 0.0)
            except (TypeError, ValueError):
                price_tick = 0.0

            qty_step = 0.0
            base_decimals = info.get('base_decimals')
            if base_decimals is not None:
                try:
                    qty_step = 10 ** (-int(base_decimals))
                except (TypeError, ValueError):
                    qty_step = 0.0
            if not qty_step:
                try:
                    qty_step = float(info.get('precision', {}).get('amount', 0.0) or 0.0)
                except (TypeError, ValueError):
                    qty_step = 0.0
            if qty_step and min_qty:
                qty_step = max(qty_step, min_qty)

            min_notional = info.get('min_notional')
            try:
                min_notional = float(min_notional) if min_notional is not None else None
            except (TypeError, ValueError):
                min_notional = None

            m = MarketInfo(
                symbol=symbol,
                base=info.get('base', symbol.split('_')[0]),
                quote=info.get('quote', 'USDT'),
                min_qty=min_qty,
                max_qty=max_qty,
                qty_step=qty_step,
                price_tick=price_tick,
                min_notional=min_notional,
                max_leverage=leverage_limits.get(symbol),
            )
            # Preserve raw metadata for downstream interval/leverage parsing.
            m.raw = info
            try:
                fi_hours = info.get("funding_interval_hours")
                if fi_hours is not None:
                    m.funding_interval_s = int(float(fi_hours) * 3600)
            except (TypeError, ValueError):
                pass
            self.markets[symbol] = m
            
        return self.markets

    async def fetch_ticker(self, symbol: str) -> Ticker:
        data = await asyncio.to_thread(self.client.fetch_ticker, symbol)
        # Data may not include 'last' for GRVT. Fall back to mark/index/close.
        bid = float(data.get('bid', 0) or data.get('best_bid_price', 0) or 0)
        ask = float(data.get('ask', 0) or data.get('best_ask_price', 0) or 0)
        last = float(
            data.get('last', 0)
            or data.get('mark_price', 0)
            or data.get('index_price', 0)
            or data.get('close', 0)
            or 0
        )
        ts = data.get('timestamp', 0) or 0
        return Ticker(symbol=symbol, bid=bid, ask=ask, last=last, timestamp=ts)

    async def fetch_balance(self) -> Dict[str, Balance]:
        # GRVT returns CCXT style balance
        # GRVT (pysdk) returns account summary flat dict?
        bal = await asyncio.to_thread(self.client.fetch_balance)
        # Raw: {'sub_account_id': ..., 'total_cross_equity': '58.36', 'cross_unrealized_pnl': '0.0', ...}
        
        result = {}
        
        # Check if it's CCXT format (Dict of Dicts) or Account Summary (Flat Dict)
        if hasattr(bal, 'get') and bal.get('total_cross_equity'):
            # It's Account Summary
            total = float(bal.get('total_cross_equity', 0) or 0)
            unrealized = float(bal.get('cross_unrealized_pnl', 0) or 0)
            # Free = Total - Maintenance Margin? Or just Total?
            # Assuming 'free' ~ total for now (simplified)
            # Or 'withdrawable'?
            # For arbitrage, we care about Equity.
            
            result['USDT'] = Balance(
                currency='USDT',
                total=total,
                free=total, # Placeholder
                used=0.0    # Placeholder
            )
            result['info'] = bal
            return result
            
        # Fallback to standard loop if it was standard
        for cur, data in bal.items():
            if cur in ('info', 'total', 'free', 'used'):
                continue
            if not isinstance(data, dict): continue
            
            result[cur] = Balance(
                currency=cur,
                total=float(data.get('total', 0) or 0),
                free=float(data.get('free', 0) or 0),
                used=float(data.get('used', 0) or 0)
            )
        return result

    async def subscribe_ticker(self, symbol: str, callback):
        """
        Subscribe to ticker updates via WebSocket.
        Uses 'book.s' channel for BBO (Best Bid/Offer).
        """
        if not GrvtCcxtWS:
            logger.error("GrvtCcxtWS module not found.")
            return

        # Ensure WS Client exists
        if not hasattr(self, 'ws_client') or not self.ws_client:
             try:
                loop = asyncio.get_running_loop()
             except:
                loop = asyncio.new_event_loop()
            
             params = {
                 'api_key': self.api_key, 
                 'private_key': self.private_key, 
                 'trading_account_id': self.subaccount_id
             }
             
             # Initialize WS Client
             # Use same environment as REST
             quiet = logging.getLogger("quiet_grvt")
             quiet.setLevel(logging.CRITICAL)
             
             self.ws_client = GrvtCcxtWS(env=self.env_enum, loop=loop, parameters=params, logger=quiet)
             await self.ws_client.initialize()
             # self._ws_running = True # Managed by client usually?

        # Callback Wrapper
        async def handle_msg(msg):
             # Format: {'feed': {'bids': [...], 'asks': [...]}, ...}
             try:
                 feed = msg.get("feed")
                 if feed:
                     b = feed.get('bids', [])
                     a = feed.get('asks', [])
                     if b and a:
                         bid_p = float(b[0]['price'])
                         ask_p = float(a[0]['price'])
                         
                         # Update Ticker
                         # Note: WS might not send 24h stats, so last/vol might be approx or missing
                         t = Ticker(
                             symbol=symbol,
                             bid=bid_p,
                             ask=ask_p,
                             last=(bid_p + ask_p) / 2, # Approx mid
                             timestamp=int(asyncio.get_running_loop().time() * 1000)
                         )
                         await callback(t)
             except Exception as e:
                 logger.error(f"GRVT WS Handler Error: {e}")

        # Subscribe
        # channel 'book.s' depth 10
        await self.ws_client.subscribe(
            stream='book.s', 
            callback=handle_msg, 
            params={'instrument': symbol, 'depth': 10}
        )
        logger.info(f"Subscribed to GRVT {symbol} (book.s)")

    async def subscribe_order_updates(self, callback):
        """
        Subscribe to private order updates ('order' and 'fill').
        """
        if not GrvtCcxtWS:
            logger.error("GrvtCcxtWS module not found.")
            return

        if not hasattr(self, 'ws_client') or not self.ws_client:
             # Just like subscribe_ticker logic
             try:
                loop = asyncio.get_running_loop()
             except:
                loop = asyncio.new_event_loop()
            
             params = {
                 'api_key': self.api_key, 
                 'private_key': self.private_key, 
                 'trading_account_id': self.subaccount_id
             }
             quiet = logging.getLogger("quiet_grvt_ws")
             quiet.setLevel(logging.CRITICAL)
             self.ws_client = GrvtCcxtWS(env=self.env_enum, loop=loop, parameters=params, logger=quiet)
             await self.ws_client.initialize()

        logger.info("Subscribing to GRVT 'order' & 'fill' streams...")
        await self.ws_client.subscribe(stream='order', callback=callback)
        await self.ws_client.subscribe(stream='fill', callback=callback)
        logger.info("Subscribed to GRVT 'order/fill' streams.")

    async def create_order(self, symbol, type, side, amount, price=None, params={}) -> Order:
        # type: OrderType.LIMIT -> 'limit'
        # side: OrderSide.BUY -> 'buy'
        t_str = type.value if isinstance(type, OrderType) else type
        s_str = side.value if isinstance(side, OrderSide) else side
        
        try:
            res = await asyncio.to_thread(
                self.client.create_order,
                symbol, t_str, s_str, amount, price, params
            )
            # Parse result
            order_id = None
            client_order_id = None
            if isinstance(res, dict):
                order_id = res.get('id') or res.get('order_id')
                client_order_id = (res.get('metadata') or {}).get('client_order_id')
                if not order_id or str(order_id) == "0x00":
                    order_id = client_order_id
            if not order_id or str(order_id) == "0x00":
                await asyncio.sleep(0.2)
                verified = await self._verify_order_created(symbol, order_id, client_order_id)
                if verified:
                    order_id = verified.id
                    if verified.client_order_id:
                        client_order_id = verified.client_order_id
                else:
                    logger.warning(
                        f"GRVT create_order verification failed for {symbol} (client_id={client_order_id})."
                    )
            return Order(
                id=str(order_id) if order_id is not None else "0x00",
                symbol=symbol,
                type=type,
                side=side,
                amount=amount,
                price=price,
                status=OrderStatus.OPEN, # Optimistic, need parsing 'status'
                client_order_id=str(client_order_id) if client_order_id is not None else None,
                raw=res
            )
        except Exception as e:
            raise ExchangeError(f"GRVT Order Failed: {e}")

    async def cancel_order(self, id: str, symbol: Optional[str] = None) -> bool:
        try:
            if id and isinstance(id, str) and not id.startswith("0x"):
                ok = await asyncio.to_thread(self.client.cancel_order, None, symbol, {"client_order_id": id})
                return bool(ok)
            ok = await asyncio.to_thread(self.client.cancel_order, id, symbol)
            if ok:
                return True
            # Retry using client_order_id if available
            ok = await asyncio.to_thread(self.client.cancel_order, None, symbol, {"client_order_id": id})
            return bool(ok)
        except Exception as e:
            raise ExchangeError(f"Cancel Failed: {e}")

    def _parse_order_payload(self, raw: Dict[str, Any], symbol_hint: Optional[str] = None) -> Order:
        legs = raw.get("legs") or []
        leg0 = legs[0] if legs else {}

        raw_type = raw.get("type") or raw.get("order_type")
        if not raw_type:
            raw_type = "market" if raw.get("is_market") else "limit"
        raw_type = str(raw_type).lower()
        if raw_type not in ("limit", "market"):
            raw_type = "limit"

        raw_side = raw.get("side")
        if not raw_side and "is_buying_asset" in leg0:
            raw_side = "buy" if leg0.get("is_buying_asset") else "sell"
        raw_side = str(raw_side).lower() if raw_side else "buy"

        symbol = raw.get("symbol") or raw.get("instrument") or leg0.get("instrument") or symbol_hint or "Unknown"

        client_order_id = None
        if isinstance(raw.get("metadata"), dict):
            client_order_id = raw["metadata"].get("client_order_id")
        if not client_order_id:
            client_order_id = raw.get("client_order_id")

        oid = raw.get("id") or raw.get("order_id") or client_order_id or "0x00"
        if oid == "0x00" and client_order_id:
            oid = client_order_id

        amount = float(raw.get("amount") or 0)
        price = float(raw.get("price") or 0)
        if (amount == 0 or price == 0) and leg0:
            if amount == 0:
                amount = float(leg0.get("size") or 0)
            if price == 0:
                price = float(leg0.get("limit_price") or 0)

        remaining = float(raw.get("remaining") or 0)
        filled = float(raw.get("filled") or 0)
        state = raw.get("state") or {}
        if remaining == 0 and isinstance(state, dict):
            book_size = state.get("book_size")
            if isinstance(book_size, list) and book_size:
                remaining = float(book_size[0] or 0)
        if filled == 0 and isinstance(state, dict):
            traded_size = state.get("traded_size")
            if isinstance(traded_size, list) and traded_size:
                filled = float(traded_size[0] or 0)

        status_raw = raw.get("status")
        if not status_raw and isinstance(state, dict):
            status_raw = state.get("status")
        status = OrderStatus.OPEN
        if status_raw:
            s = str(status_raw).lower()
            if "cancel" in s:
                status = OrderStatus.CANCELED
            elif s in ("closed", "filled"):
                status = OrderStatus.CLOSED
            elif s in ("open", "pending", "new", "in-progress"):
                status = OrderStatus.OPEN

        return Order(
            id=str(oid) if oid is not None else "0x00",
            symbol=symbol,
            type=OrderType(raw_type),
            side=OrderSide(raw_side) if raw_side in ("buy", "sell") else OrderSide.BUY,
            amount=amount,
            price=price,
            remaining=remaining,
            filled=filled,
            status=status,
            client_order_id=str(client_order_id) if client_order_id is not None else None,
            raw=raw,
        )

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Order]:
        raw = await asyncio.to_thread(self.client.fetch_open_orders, symbol)
        orders = []
        for r in raw:
            orders.append(self._parse_order_payload(r, symbol_hint=symbol))
        return orders

    async def _verify_order_created(self, symbol: str, order_id: Optional[str], client_order_id: Optional[str]) -> Optional[Order]:
        try:
            open_orders = await self.fetch_open_orders(symbol)
        except Exception as e:
            logger.warning(f"GRVT fetch_open_orders verify failed: {e}")
            return None
        for o in open_orders:
            if order_id and str(o.id) == str(order_id):
                return o
            if client_order_id and (
                str(o.client_order_id) == str(client_order_id) or str(o.id) == str(client_order_id)
            ):
                return o
        return None
    
    # Placeholders for now
    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        # Based on Legacy logic: fetch_ticker + fields
        try:
            ticker = await asyncio.to_thread(self.client.fetch_ticker, symbol)
            rate = 0.0
            if ticker:
                rate = float(ticker.get('funding_rate_curr') or ticker.get('funding_rate_8h_curr') or ticker.get('funding_rate') or 0.0)
            
            # Timestamp (next funding)
            next_time = int(ticker.get('next_funding_time', 0))
            # Normalize to ms (some responses are ns)
            if next_time > 10**13:
                next_time = next_time // 1_000_000
            
            return FundingRate(symbol, rate, next_time)
        except Exception as e:
            logger.error(f"Fetch Funding Rate Failed: {e}")
            return FundingRate(symbol, 0.0, 0) 

    async def fetch_positions(self) -> List[Position]:
        try:
            raw = await asyncio.to_thread(self.client.fetch_positions)
            positions = []
            for p in raw:
                size = float(p.get('size') or p.get('contracts') or 0)
                if size == 0: continue
                
                symbol = p.get('symbol') or p.get('instrument')
                entry_price = float(p.get('entry_price') or p.get('entryPrice') or 0)
                unrealized_pnl = float(p.get('unrealizedPnL') or 0)
                side = OrderSide.BUY if size > 0 else OrderSide.SELL
                
                # Leverage? GRVT positions might not show leverage directly?
                # Legacy didn't parse leverage from position.
                leverage = 1.0 # Placeholder
                
                positions.append(Position(
                    symbol=symbol,
                    side=side,
                    amount=abs(size),
                    leverage=leverage,
                    entry_price=entry_price,
                    unrealized_pnl=unrealized_pnl
                ))
            return positions
        except Exception as e:
            logger.error(f"Fetch Positions Failed: {e}")
            return []
        
    async def fetch_order(self, id: str, symbol: Optional[str] = None) -> Order:
        try:
            params = {}
            order_id = id
            if id and isinstance(id, str) and not id.startswith("0x"):
                params["client_order_id"] = id
                order_id = None
            r = await asyncio.to_thread(self.client.fetch_order, order_id, params)
            raw = r.get("result") if isinstance(r, dict) and "result" in r else r
            if not raw:
                raise OrderNotFound(f"Order {id} not found.")
            return self._parse_order_payload(raw, symbol_hint=symbol)
        except Exception as e:
            # Fallback to Open Orders scan?
            logger.warning(f"Fetch Order {id} failed: {e}. Checking Open Orders...")
            open_orders = await self.fetch_open_orders(symbol)
            for o in open_orders:
                if o.id == str(id): return o
            raise OrderNotFound(f"Order {id} not found.")
        
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            if self.disable_leverage_update:
                if not self._leverage_skip_logged:
                    logger.info("Skipping GRVT leverage update (disabled).")
                    self._leverage_skip_logged = True
                return True
            if self.use_position_config:
                return await self.set_position_config(symbol, leverage, self.position_margin_type)
            if self._leverage_deprecated:
                if not self._leverage_skip_logged:
                    logger.info("Skipping GRVT leverage update (deprecated).")
                    self._leverage_skip_logged = True
                return True
            # Logic from Legacy: _auth_and_post
            acc_id = self.client.get_trading_account_id()
            payload = {
                "sub_account_id": acc_id,
                "instrument": symbol, # Assume mapped
                "leverage": str(leverage)
            }
            # Determine URL
            base_url = "https://trades.grvt.io"
            if self.env_str == 'TESTNET': base_url = "https://trades.testnet.grvt.io"
            url = f"{base_url}/full/v1/set_initial_leverage"
            
            if hasattr(self.client, '_auth_and_post'):
                 resp = await asyncio.to_thread(self.client._auth_and_post, url, payload=payload)
                 if resp.get('success'):
                     return True
                 code = resp.get('code')
                 message = str(resp.get('message') or "").lower()
                 if code == 2106 or "deprecated" in message:
                     self._leverage_deprecated = True
                     return True
            
            logger.error(f"Set GRVT Leverage Failed: {symbol} -> {leverage}x")
            return False
        except Exception as e:
            logger.error(f"Set Leverage Failed: {e}")
            return False

    async def set_position_config(
        self, symbol: str, leverage: int | float, margin_type: str = "CROSS"
    ) -> bool:
        if not self.private_key:
            logger.error("GRVT position config requires private_key.")
            return False
        if not get_EIP712_domain_data or not GrvtEnv:
            logger.error("GRVT signing utilities not available.")
            return False

        margin_type = str(margin_type or "CROSS").upper()
        try:
            lev_dec = Decimal(str(leverage))
            if lev_dec == lev_dec.to_integral():
                leverage_str = str(lev_dec.to_integral())
            else:
                leverage_str = format(lev_dec.normalize(), "f").rstrip("0").rstrip(".")
        except (InvalidOperation, TypeError):
            leverage_str = str(leverage)
        cache_key = f"{symbol}:{margin_type}:{leverage_str}"
        if self._position_config_cache.get(symbol) == cache_key:
            return True

        env = GrvtEnv.TESTNET if self.env_str == "TESTNET" else GrvtEnv.PROD
        domain_base = get_EIP712_domain_data(env)
        chain_id_val = int(domain_base.get("chainId") or 0)
        chain_id = str(chain_id_val)
        if self.env_str == "TESTNET":
            chain_name = "GRVT Testnet"
        elif self.env_str == "PROD":
            chain_name = "GRVT Mainnet"
        else:
            chain_name = "GRVT Exchange"
        domain_names = [str(domain_base.get("name") or "GRVT Exchange"), chain_name, "GRVT Exchange"]
        domain_candidates: list[dict[str, str | int]] = []
        seen_domains: set[tuple[str, int]] = set()
        for name in domain_names:
            name = str(name)
            for cid in (chain_id_val, 0):
                key = (name, int(cid))
                if key in seen_domains:
                    continue
                domain_candidates.append({"name": name, "version": "0", "chainId": int(cid)})
                seen_domains.add(key)

        instrument = (self.client.markets.get(symbol) or {})
        asset_id = instrument.get("instrument_hash")
        if not asset_id:
            logger.error(f"GRVT set_position_config missing instrument_hash for {symbol}.")
            return False

        mt_val = 1 if margin_type == "ISOLATED" else 2
        try:
            lev_dec = Decimal(str(leverage))
            if lev_dec != lev_dec.to_integral():
                logger.warning(f"GRVT set_position_config leverage not int: {leverage}")
            leverage_int = int(lev_dec.to_integral_value())
        except (InvalidOperation, TypeError, ValueError):
            leverage_int = int(float(leverage))
        leverage_variants: list[tuple[int, str]] = []
        try:
            leverage_scaled = int(Decimal(str(leverage)) * Decimal(1_000_000))
            leverage_variants.append((leverage_scaled, "int*1e6"))
        except (InvalidOperation, TypeError, ValueError):
            pass
        leverage_variants.append((leverage_int, "int"))

        types = {
            "SetSubAccountPositionMarginConfig": [
                {"name": "subAccountID", "type": "uint64"},
                {"name": "asset", "type": "uint256"},
                {"name": "marginType", "type": "uint8"},
                {"name": "leverage", "type": "int32"},
                {"name": "nonce", "type": "uint32"},
                {"name": "expiration", "type": "int64"},
            ],
        }
        asset_variants: list[tuple[object, str]] = [(asset_id, "hex")]
        if isinstance(asset_id, str) and asset_id.startswith("0x"):
            try:
                asset_int = int(asset_id, 16)
                asset_variants.append((asset_int, "int"))
            except ValueError:
                pass
        message_base = {
            "subAccountID": int(self.subaccount_id),
            "marginType": mt_val,
        }

        def is_sig_error(resp: dict) -> bool:
            code = resp.get("code")
            return code in {2000, 2001, 2002, 2004, 2005, 2006, 2007, 2008}

        base_url = "https://trades.grvt.io"
        if self.env_str == 'TESTNET':
            base_url = "https://trades.testnet.grvt.io"
        full_url = f"{base_url}/full/v1/set_position_config"

        from eth_account import Account
        from eth_account.messages import encode_typed_data

        sig_error_variants: list[str] = []
        for domain in domain_candidates:
            for asset_value, asset_tag in asset_variants:
                for lev_value, lev_tag in leverage_variants:
                    self._last_position_config_variant = (
                        f"SetSubAccountPositionMarginConfig:{domain['name']}@{domain['chainId']}"
                        f" asset={asset_tag} leverage={lev_tag}"
                    )
                    nonce = rand_uint32() if rand_uint32 else int(time.time() * 1000) % (2**32)
                    expiration = int(time.time_ns() + int(24 * 3600 * 1_000_000_000))
                    message = dict(message_base)
                    message["asset"] = asset_value
                    message["leverage"] = lev_value
                    message["nonce"] = nonce
                    message["expiration"] = expiration
                    try:
                        signable = encode_typed_data(domain, types, message)
                    except Exception as e:
                        logger.warning(
                            f"GRVT set_position_config skip domain {domain['name']} ({asset_tag}/{lev_tag}): {e}"
                        )
                        continue
                    signed = Account.sign_message(signable, self.private_key)
                    signature = {
                        "signer": Account.from_key(self.private_key).address,
                        "r": "0x" + signed.r.to_bytes(32, byteorder="big").hex(),
                        "s": "0x" + signed.s.to_bytes(32, byteorder="big").hex(),
                        "v": signed.v,
                        "expiration": str(expiration),
                        "nonce": nonce,
                    }
                    payload = {
                        "sub_account_id": str(self.subaccount_id),
                        "instrument": symbol,
                        "margin_type": margin_type,
                        "leverage": leverage_str,
                        "signature": signature,
                    }
                    url = full_url
                    redacted_sig = dict(signature)
                    redacted_sig["r"] = "<redacted>"
                    redacted_sig["s"] = "<redacted>"
                    redacted_payload = {**payload, "signature": redacted_sig}
                    self._last_position_config_payload = redacted_payload
                    try:
                        resp = await asyncio.to_thread(self.client._auth_and_post, url, payload=payload)
                    except Exception as e:
                        self._last_position_config_response = {"error": str(e)}
                        logger.error(f"GRVT set_position_config failed: {e}")
                        return False
                    if isinstance(resp, dict):
                        self._last_position_config_response = resp
                        ack = resp.get("ack")
                        if isinstance(resp.get("result"), dict):
                            ack = resp["result"].get("ack", ack)
                        if ack:
                            self._position_config_cache[symbol] = cache_key
                            logger.info(f"GRVT set_position_config ok via {self._last_position_config_variant}")
                            return True
                        if is_sig_error(resp):
                            sig_error_variants.append(self._last_position_config_variant)
                            continue
                        logger.warning(f"GRVT set_position_config failed: {resp}")
                        return False
                    logger.warning(f"GRVT set_position_config unexpected response: {resp}")
                    return False
        if sig_error_variants:
            logger.warning(
                "GRVT set_position_config signature mismatch; last_variant=%s",
                sig_error_variants[-1],
            )
        return False
