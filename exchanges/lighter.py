import asyncio
import os
import logging
import inspect
import time
from typing import Dict, List, Optional, Any
from ..base import AbstractExchange
from ..models import (
    Ticker, Order, OrderType, OrderSide, OrderStatus, 
    Balance, Position, FundingRate, MarketInfo
)
from ..errors import ExchangeError, NetworkError, AuthenticationError, OrderNotFound

# Import dependent modules (assuming they are in path as per original bot)
try:
    import lighter
    from lighter.configuration import Configuration
    from lighter.api_client import ApiClient
except ImportError:
    lighter = None

logger = logging.getLogger("SharedLib.Lighter")

class LighterExchange(AbstractExchange):
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.wallet_address = config.get('wallet_address')
        self.private_key = config.get('private_key')
        self.public_key = config.get('public_key')
        self.api_key_index = config.get('api_key_index', 0)
        self.env_str = config.get('env', 'MAINNET')
        self.auth_token = None
        
        self.client = None # SignerClient
        self.rest_client = None # ApiClient/OrderApi/FundingApi if needed
        self.account_index = -1
        self.config_obj = None # Lighter Config
        self.markets_map = {} # Internal map ID->Symbol, Symbol->ID
        
        # State
        self._ticker_cache: Dict[str, Ticker] = {}
        self._funding_cache: Dict[str, FundingRate] = {}
        self._funding_raw_ts: Dict[str, int] = {}
        self._ws_task = None
        self._ws_subs: Dict[str, List] = {}
        self._ws_client = None
        self.funding_interval_s = 3600
        self._error_cooldown_until = 0.0
        self._error_cooldown_reason = ""
        try:
            self._error_cooldown_s = float(os.getenv("LIGHTER_ERROR_COOLDOWN_S", "120"))
        except Exception:
            self._error_cooldown_s = 120.0
        try:
            self._error_cooldown_auth_s = float(os.getenv("LIGHTER_ERROR_COOLDOWN_AUTH_S", "60"))
        except Exception:
            self._error_cooldown_auth_s = 60.0
        if isinstance(config, dict):
            try:
                self.funding_interval_s = int(config.get("funding_interval_s", self.funding_interval_s))
            except (TypeError, ValueError):
                self.funding_interval_s = 3600
        
    async def initialize(self):
        if not lighter:
            raise ExchangeError("Lighter SDK not installed.")
            
        self.config_obj = Configuration()
        host = "https://mainnet.zklighter.elliot.ai" if self.env_str == "MAINNET" else "https://testnet.zklighter.elliot.ai"
        self.config_obj.host = host
        
        # 1. Discover Account Index
        if self.wallet_address:
            await self._discover_account_index()
            
        # 2. Initialize SignerClient
        if self.private_key and self.account_index >= 0:
            pk = self.private_key
            if pk.startswith("0x"): pk = pk[2:]
            
            sig = inspect.signature(lighter.SignerClient)
            init_kwargs = { 
                "url": self.config_obj.host, 
                "account_index": self.account_index, 
                "api_private_keys": {self.api_key_index: pk} 
            }
            # Handle variable arguments in SignerClient (legacy vs new)
            valid_kwargs = {k: v for k, v in init_kwargs.items() if k in sig.parameters}
            self.client = lighter.SignerClient(**valid_kwargs)
            
            # 3. Auth Token
            try:
                auth_res = self.client.create_auth_token_with_expiry()
                # Check format
                token = auth_res[0] if isinstance(auth_res, tuple) and len(auth_res) > 1 and not auth_res[1] else None
                if not token:
                     # Maybe it returned just token string or threw error inside tuple?
                     if isinstance(auth_res, str): token = auth_res
                     elif isinstance(auth_res, tuple) and not auth_res[1]: token = auth_res[0]
                     else: raise AuthenticationError(f"Auth Token Gen Failed: {auth_res}")
                
                self.auth_token = token
                self.config_obj.api_key['Authorization'] = token
                self.config_obj.access_token = token
                if self.client.api_client and self.client.api_client.configuration:
                    self.client.api_client.configuration.api_key['Authorization'] = token
                    self.client.api_client.configuration.access_token = token
                logger.info("Lighter Initialized & Authenticated.")
            except Exception as e:
                raise AuthenticationError(f"Lighter Auth Failed: {e}")
        
        await self.load_markets()
        self.is_initialized = True

    async def _discover_account_index(self):
        import aiohttp
        url = f"{self.config_obj.host}/api/v1/account?by=l1_address&value={self.wallet_address}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"accept": "application/json"}, timeout=10) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    if data and data.get('accounts'):
                        self.account_index = int(data['accounts'][0]['index'])
        except Exception as e:
            logger.warning(f"Lighter Account Discovery Failed: {e}")

    async def load_markets(self) -> Dict[str, MarketInfo]:
        """
        Fetches market data from two separate endpoints to build a comprehensive map of
        market IDs, symbols, and trading rules. Returns a set of available symbols.
        """
        logger.info("Loading Lighter markets and rules (Legacy Mode)...")
        self.markets = {}
        self.markets_map = {}

        try:
            # 1. Explorer API (ID Mapping)
            explorer_url = "https://explorer.elliot.ai/api/markets"
            import aiohttp
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(explorer_url, headers={"accept": "application/json"}, timeout=10) as response:
                        if response.status == 200:
                            markets_data = await response.json()
                            for item in markets_data:
                                symbol = item.get('symbol', '').split('/')[0]
                                market_id = item.get('market_index')
                                if symbol and market_id is not None:
                                    self.markets_map[market_id] = symbol
                                    self.markets_map[symbol] = market_id
                        else:
                            logger.warning(f"Explorer API failed {response.status}, skipping ID map.")
            except Exception as e:
                logger.warning(f"Explorer API failed, skipping ID map: {e}")

            # 2. Order Book Details API (preferred for margin fractions)
            orderbooks_data = None
            async with aiohttp.ClientSession() as session:
                try:
                    orderbooks_url = f"{self.config_obj.host}/api/v1/orderBookDetails?filter=perp"
                    async with session.get(orderbooks_url, headers={"accept": "application/json"}, timeout=10) as response:
                        response.raise_for_status()
                        orderbooks_data = await response.json()
                except Exception as e:
                    logger.warning(f"OrderBookDetails API failed: {e}")

                if not orderbooks_data:
                    orderbooks_url = f"{self.config_obj.host}/api/v1/orderBooks?filter=perp"
                    async with session.get(orderbooks_url, headers={"accept": "application/json"}, timeout=10) as response:
                        response.raise_for_status()
                        orderbooks_data = await response.json()

            if orderbooks_data:
                order_items = []
                if orderbooks_data.get("order_book_details"):
                    order_items = orderbooks_data.get("order_book_details", [])
                elif orderbooks_data.get("order_books"):
                    order_items = orderbooks_data.get("order_books", [])

                for item in order_items:
                    if item.get('market_type') and item.get('market_type') != 'perp':
                        continue
                    # Parse Symbol: "ETH-PERP/USDC" -> "ETH" or "ETH-PERP"
                    raw_sym = item.get('symbol', '')
                    base = raw_sym.split('-')[0].split('/')[0]
                    
                    # Market ID
                    market_id = item.get('market_id')
                    
                    # Full Symbol for Bot: "ETH-USDT" (we normalize to USDT for bot consistency)
                    symbol = f"{base}-USDT"
                    
                    if base and market_id is not None:
                        self.markets_map[market_id] = symbol 
                        self.markets_map[symbol] = market_id
                        
                        # Create MarketInfo
                        size_decimals = item.get('size_decimals') or item.get('supported_size_decimals')
                        price_decimals = item.get('price_decimals') or item.get('supported_price_decimals')
                        qty_step = 0.0
                        price_tick = 0.0
                        try:
                            if size_decimals is not None:
                                qty_step = 10 ** (-int(size_decimals))
                        except (TypeError, ValueError):
                            qty_step = 0.0
                        try:
                            if price_decimals is not None:
                                price_tick = 10 ** (-int(price_decimals))
                        except (TypeError, ValueError):
                            price_tick = 0.0

                        min_qty = float(item.get('min_base_amount', 0.0) or 0.0)
                        min_notional = None
                        try:
                            min_quote = item.get('min_quote_amount')
                            if min_quote is not None:
                                min_notional = float(min_quote)
                        except (TypeError, ValueError):
                            min_notional = None

                        max_leverage = None
                        try:
                            min_imf = item.get('min_initial_margin_fraction')
                            if min_imf:
                                max_leverage = 10_000 / float(min_imf)
                        except (TypeError, ValueError):
                            max_leverage = None

                        m_info = MarketInfo(
                            symbol=symbol,
                            base=base,
                            quote='USDT', 
                            min_qty=min_qty,
                            qty_step=qty_step,
                            price_tick=price_tick,
                            min_notional=min_notional,
                            max_leverage=max_leverage,
                        )
                        m_info.raw = item
                        m_info.id = market_id
                        try:
                            interval_s = None
                            if item.get("funding_interval_s") is not None:
                                interval_s = int(float(item["funding_interval_s"]))
                            elif item.get("funding_interval_hours") is not None:
                                interval_s = int(float(item["funding_interval_hours"]) * 3600)
                            if interval_s:
                                m_info.funding_interval_s = interval_s
                            else:
                                m_info.funding_interval_s = int(self.funding_interval_s)
                        except (TypeError, ValueError):
                            m_info.funding_interval_s = int(self.funding_interval_s)
                        
                        # Store MarketInfo OBJECT directly
                        self.markets[symbol] = m_info
                            
            logger.info(f"Lighter Markets Loaded: {len(self.markets)}")
            return self.markets
            
        except Exception as e:
            logger.error(f"Load Markets Failed (Legacy): {e}")
            return {}

    async def fetch_ticker(self, symbol: str) -> Ticker:
        # Return from cache if available
        if symbol in self._ticker_cache:
            t = self._ticker_cache[symbol]
            if t.last == 0 and t.bid and t.ask:
                t.last = (t.bid + t.ask) / 2
            if os.environ.get("LIGHTER_TICKER_DEBUG") == "1":
                logger.info(f"[LGHT] fetch_ticker cache {symbol} bid={t.bid} ask={t.ask} last={t.last}")
            return t
        if os.environ.get("LIGHTER_TICKER_DEBUG") == "1":
            logger.info(f"[LGHT] fetch_ticker missing cache for {symbol}")
        return Ticker(symbol, 0, 0, 0, 0) # Empty if no WS data yet

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        # Direct cache access
        if symbol in self._funding_cache:
            item = self._funding_cache[symbol]
            logger.debug(f"fetch_funding_rate({symbol}) HIT: {item}")
            return item
            
        logger.debug(f"fetch_funding_rate({symbol}) MISS. Keys: {list(self._funding_cache.keys())}")
        return FundingRate(symbol, 0.0, 0)

    # --- WebSocket ---
    
    async def subscribe_ticker(self, symbol: str, callback):
        if not self._ws_task:
            self._ws_task = asyncio.create_task(self._ws_loop())
            
        if symbol not in self._ws_subs:
            self._ws_subs[symbol] = []
            # If WS is already connected, send subscribe immediately
            if self._ws_client and self._ws_client.open:
                mid = self.markets_map.get(symbol)
                if mid is not None:
                     import json
                     await self._ws_client.send(json.dumps({"type": "subscribe", "channel": f"order_book/{mid}"}))
                     await self._ws_client.send(json.dumps({"type": "subscribe", "channel": f"market_stats/{mid}"}))

        self._ws_subs[symbol].append(callback)
        logger.info(f"Subscribed to {symbol} (Callback registered)")

    async def _ws_loop(self):
        import websockets
        import json
        
        ws_url = f"{self.config_obj.host.replace('https', 'wss')}/stream"
        logger.info(f"Connecting to Lighter WS: {ws_url}")
        try:
            ping_interval = float(os.getenv("LIGHTER_WS_PING_INTERVAL_S", "20"))
        except Exception:
            ping_interval = 20.0
        try:
            ping_timeout = float(os.getenv("LIGHTER_WS_PING_TIMEOUT_S", "15"))
        except Exception:
            ping_timeout = 15.0
        
        while True:
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=ping_interval,
                    ping_timeout=ping_timeout,
                ) as ws:
                    self._ws_client = ws
                    # Subscribe to requested markets only
                    ids_to_sub = set()
                    for sym in self._ws_subs.keys():
                        mid = self.markets_map.get(sym)
                        if mid is not None:
                             ids_to_sub.add(mid)

                    for mid in ids_to_sub:
                        await ws.send(json.dumps({"type": "subscribe", "channel": f"order_book/{mid}"}))
                        await ws.send(json.dumps({"type": "subscribe", "channel": f"market_stats/{mid}"}))
                        
                    logger.info(f"Lighter WS Connected & Subscribed to {len(ids_to_sub)} markets")
                    
                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                            if isinstance(data, dict) and data.get("type") == "ping":
                                await ws.send(json.dumps({"type": "pong"}))
                                continue
                            channel = data.get('channel', '')
                            
                            # Parse Channel
                            # "market_stats/0" or "order_book/0"
                            if '/' not in channel and ':' not in channel: continue
                            
                            sep = '/' if '/' in channel else ':'
                            ctype, mid_str = channel.split(sep)
                            mid = int(mid_str)
                            symbol = self.markets_map.get(mid)
                            if not symbol: continue
                            
                            # Update Cache
                            if symbol not in self._ticker_cache:
                                self._ticker_cache[symbol] = Ticker(symbol, 0, 0, 0, 0)
                            
                            t = self._ticker_cache[symbol]
                            updated = False
                            
                            if 'order_book' in ctype:
                                ob = data.get('order_book', {})
                                bids = ob.get('bids', [])
                                asks = ob.get('asks', [])
                                if bids: t.bid = float(bids[0]['price'])
                                if asks: t.ask = float(asks[0]['price'])
                                if t.last == 0 and t.bid and t.ask:
                                    t.last = (t.bid + t.ask) / 2
                                updated = True
                                
                            elif 'market_stats' in ctype:
                                stats = data.get('market_stats', {})
                                logger.debug(f"[ws] {symbol} (mid={mid}) stats: {stats}") # DEBUG: View raw stats
                                if 'last_trade_price' in stats:
                                    t.last = float(stats['last_trade_price'])
                                    updated = True
                                elif os.environ.get("LIGHTER_TICKER_DEBUG") == "1":
                                    logger.info(f"[LGHT] market_stats missing last_trade_price for {symbol}: {stats}")
                                
                                # Funding
                                if 'funding_rate' in stats:
                                    fr = float(stats['funding_rate'])
                                    ts_raw = int(stats.get('funding_timestamp', 0) or 0)
                                    if ts_raw and ts_raw < 10_000_000_000:
                                        ts_raw = ts_raw * 1000
                                    self._funding_raw_ts[symbol] = ts_raw
                                    next_time = ts_raw
                                    now_ms = int(time.time() * 1000)
                                    if next_time and next_time < now_ms - 60_000:
                                        next_time = next_time + int(self.funding_interval_s * 1000)
                                    self._funding_cache[symbol] = FundingRate(symbol, fr, next_time)
                                    
                            if updated:
                                t.timestamp = int(asyncio.get_running_loop().time() * 1000)
                                if os.environ.get("LIGHTER_TICKER_DEBUG") == "1":
                                    logger.info(f"[LGHT] ws update {symbol} bid={t.bid} ask={t.ask} last={t.last}")
                                # Notify Callbacks
                                if symbol in self._ws_subs:
                                    for cb in self._ws_subs[symbol]:
                                        if asyncio.iscoroutinefunction(cb):
                                            await cb(t)
                                        else:
                                            cb(t)
                                            
                        except Exception as e:
                            logger.error(f"WS Parse Error: {e}")
                            
            except Exception as e:
                logger.error(f"WS Connection Error: {e}")
                await asyncio.sleep(5)

    def _cooldown_remaining(self) -> float:
        try:
            return max(0.0, self._error_cooldown_until - time.time())
        except Exception:
            return 0.0

    def _format_error_detail(self, err) -> str:
        try:
            if err is None:
                return ""
            if isinstance(err, dict):
                return str({k: err.get(k) for k in ("status", "status_code", "code", "message", "body", "text", "url") if k in err})
            if isinstance(err, (list, tuple)):
                return str(err)
            for key in ("status", "status_code", "code", "message", "body", "text"):
                if hasattr(err, key):
                    return f"{key}={getattr(err, key)}"
            return str(err)
        except Exception:
            return str(err)

    def _extract_endpoint(self, err) -> str:
        try:
            if err is None:
                return ""
            if isinstance(err, dict) and err.get("url"):
                return str(err.get("url"))
            if hasattr(err, "url"):
                return str(getattr(err, "url"))
            req_info = getattr(err, "request_info", None)
            if req_info is not None:
                real_url = getattr(req_info, "real_url", None)
                if real_url:
                    return str(real_url)
            return ""
        except Exception:
            return ""

    def _maybe_set_error_cooldown(self, err, context: str, cooldown_s: float = None) -> None:
        detail = self._format_error_detail(err)
        text = detail.lower()
        if "401" in text or "unauthorized" in text or "429" in text or "too many" in text:
            use_cooldown = cooldown_s if cooldown_s is not None else float(self._error_cooldown_s or 0)
            self._error_cooldown_until = time.time() + float(use_cooldown or 0)
            self._error_cooldown_reason = f"{context}:{detail}"
            logger.warning(
                "Lighter cooldown set for %.0fs due to %s",
                float(use_cooldown or 0),
                self._error_cooldown_reason,
            )

    def _is_auth_error(self, err) -> bool:
        detail = self._format_error_detail(err)
        text = detail.lower()
        return "401" in text or "unauthorized" in text

    async def _refresh_auth_token(self, reason: str) -> bool:
        if not self.client:
            return False
        try:
            auth_res = self.client.create_auth_token_with_expiry()
            token = auth_res[0] if isinstance(auth_res, tuple) and len(auth_res) > 1 and not auth_res[1] else None
            if not token:
                if isinstance(auth_res, str):
                    token = auth_res
                elif isinstance(auth_res, tuple) and not auth_res[1]:
                    token = auth_res[0]
                else:
                    raise AuthenticationError(f"Auth Token Gen Failed: {auth_res}")
            self.auth_token = token
            self.config_obj.api_key['Authorization'] = token
            self.config_obj.access_token = token
            if self.client.api_client and self.client.api_client.configuration:
                self.client.api_client.configuration.api_key['Authorization'] = token
                self.client.api_client.configuration.access_token = token
            logger.warning("Lighter auth refreshed (%s).", reason)
            return True
        except Exception as e:
            logger.warning("Lighter auth refresh failed (%s): %s", reason, e)
            return False

    async def _handle_lighter_error(self, err, context: str) -> None:
        if self._is_auth_error(err):
            endpoint = self._extract_endpoint(err)
            if endpoint:
                logger.warning("Lighter auth error context=%s endpoint=%s", context, endpoint)
            await self._refresh_auth_token(context)
            self._maybe_set_error_cooldown(err, context, self._error_cooldown_auth_s)
            return
        self._maybe_set_error_cooldown(err, context)

    async def create_order(self, symbol, type, side, amount, price=None, params={}) -> Order:
        remaining = self._cooldown_remaining()
        if remaining > 0:
            raise ExchangeError(
                f"Lighter cooldown active ({remaining:.0f}s) reason={self._error_cooldown_reason}"
            )
        market_id = self.markets_map.get(symbol)
        if market_id is None:
             # Try fallback
             if 'ETH' in symbol: market_id = 0
             elif 'BTC' in symbol: market_id = 1
             else: raise ExchangeError(f"Unknown symbol {symbol}")

            
        is_ask = (side == OrderSide.SELL)
        # Price must be int? Lighter expects raw quantized values or float?
        # SignerClient usually handles decimal conversion if OrderApi is wrapped?
        # Original bot: `create_order(market_id, is_ask, size, price, ...)`
        # Price 0 for Market?
        
        try:
            # Generate client_order_index (Timestamp ms)
            import time
            client_order_index = int(time.time() * 1000)
            
            # Atomic Units Conversion (use market metadata if available)
            size_decimals = 6
            price_decimals = 2
            m_info = self.markets.get(symbol)
            if m_info and getattr(m_info, "raw", None):
                raw = m_info.raw
            try:
                size_decimals = int(raw.get("supported_size_decimals", size_decimals))
            except (TypeError, ValueError):
                pass
            try:
                price_decimals = int(raw.get("supported_price_decimals", price_decimals))
            except (TypeError, ValueError):
                pass
            
            amount_int = int(amount * (10 ** size_decimals))
            if amount_int < 1:
                raise ExchangeError(
                    "Lighter Order Error: OrderSize should not be less than 1 "
                    f"(amount={amount}, size_decimals={size_decimals}, amount_int={amount_int})"
                )

            is_market = type == OrderType.MARKET or (hasattr(type, "value") and type.value == "market")
            if is_market and (price is None or float(price or 0) <= 0):
                max_slippage = 0.01
                if isinstance(self.config, dict):
                    try:
                        max_slippage = float(self.config.get("slippage", max_slippage))
                    except Exception:
                        pass
                res = await self.client.create_market_order_limited_slippage(
                    market_index=market_id,
                    client_order_index=client_order_index,
                    base_amount=amount_int,
                    max_slippage=max_slippage,
                    is_ask=is_ask,
                )
                order_id = "unknown"
                client_order_id = str(client_order_index)
                predicted_ms = None
                if isinstance(res, tuple):
                    tx = res[0]
                    resp = res[1] if len(res) > 1 else None
                    if hasattr(tx, 'order_index'):
                        order_id = str(tx.order_index)
                    if hasattr(resp, 'predicted_execution_time_ms'):
                        predicted_ms = resp.predicted_execution_time_ms
                if len(res) > 2 and res[2]:
                    await self._handle_lighter_error(res[2], "create_market")
                    raise ExchangeError(f"Lighter Order Error: {res[2]}")
                elif isinstance(res, dict):
                    order_id = str(res.get('id') or res.get('order_id') or client_order_id)

                if order_id == "unknown":
                    await asyncio.sleep(0.2)
                    verified = await self._verify_order_created(symbol, order_id, client_order_id)
                    if verified:
                        order_id = verified.id
                    else:
                        logger.warning(
                            f"Lighter create_order verification failed for {symbol} (client_id={client_order_id})."
                        )

                return Order(
                    id=order_id if order_id != "unknown" else client_order_id,
                    symbol=symbol,
                    type=type,
                    side=side,
                    amount=amount,
                    price=price,
                    status=OrderStatus.OPEN,
                    client_order_id=client_order_id,
                    timestamp=int(predicted_ms) if predicted_ms else None,
                    raw=res
                )

            price_int = int((price or 0) * (10 ** price_decimals))
            if price_int < 1:
                raise ExchangeError(
                    "Lighter Order Error: OrderPrice should not be less than 1 "
                    f"(price={price}, price_decimals={price_decimals}, price_int={price_int})"
                )
            
            # Determine TIF
            # Market -> IOC, Limit -> GTC
            # Determine TIF
            # Market -> IOC, Limit -> GTC/GTT
            # Default to 0 (GTC equivalent?)
            tif = 0 
            if type == OrderType.MARKET or (type.value == 'market'):
                tif = getattr(self.client, 'ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL', 3)
            else:
                 # Limit Order default TIF (align with SDK examples)
                 if hasattr(self.client, 'ORDER_TIME_IN_FORCE_GOOD_TILL_TIME'):
                     tif = self.client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
                 elif hasattr(self.client, 'ORDER_TIME_IN_FORCE_POST_ONLY'):
                     tif = self.client.ORDER_TIME_IN_FORCE_POST_ONLY
                 elif hasattr(self.client, 'ORDER_TIME_IN_FORCE_GOOD_TILL_CANCEL'):
                     tif = self.client.ORDER_TIME_IN_FORCE_GOOD_TILL_CANCEL

            # Determine Order Type (MUST BE INT)
            otype = 0 # Default Limit?
            if type == OrderType.LIMIT:
                otype = getattr(self.client, 'ORDER_TYPE_LIMIT', 0)
            else:
                otype = getattr(self.client, 'ORDER_TYPE_MARKET', 2) # Guess 2? Or 1?
                # Probe showed ORDER_TYPE_MARKET exists.
            
            # SignerClient create_order signature requires 'market_index', 'client_order_index', 'base_amount', 'price', 'is_ask', 'order_type', 'time_in_force'
            # Note: Signature probe showed different order?
            # (self, market_index, client_order_index, base_amount, price, is_ask, order_type, time_in_force)
            # We use kwargs so order shouldn't matter if python wrapper handles it.
            create_kwargs = dict(
                market_index=market_id,
                is_ask=is_ask,
                base_amount=amount_int,
                price=price_int,
                order_type=otype,
                client_order_index=client_order_index,
                time_in_force=tif,
            )
            if tif == getattr(self.client, 'ORDER_TIME_IN_FORCE_GOOD_TILL_TIME', None):
                expiry_default = getattr(self.client, "DEFAULT_28_DAY_ORDER_EXPIRY", -1)
                create_kwargs["order_expiry"] = expiry_default
            if otype == getattr(self.client, 'ORDER_TYPE_MARKET', None):
                create_kwargs["order_expiry"] = getattr(self.client, "DEFAULT_IOC_EXPIRY", 0)
            # Do not set order_expiry explicitly; SDK default (-1) is accepted on mainnet.

            res = await self.client.create_order(**create_kwargs)
            # Res: {'id': ..., 'status': ...}
            # SDK might return tuple (tx_hash, order_id_str, error) or dict?
            # Legacy code said: tx, tx_hash, err = await client.create_order(...)
            # Oh! Legacy code calls `self.client.create_order` and unpacks 3 values!
            # My current code: res = await ...
            # If it returns a tuple, `res` will be a tuple.
            # I need to handle tuple return!
            
            order_id = "unknown"
            client_order_id = str(client_order_index)
            predicted_ms = None
            if isinstance(res, tuple):
                # res: (CreateOrder, RespSendTx, err)
                tx = res[0]
                resp = res[1] if len(res) > 1 else None
                if hasattr(tx, 'order_index'):
                    order_id = str(tx.order_index)
                if hasattr(resp, 'predicted_execution_time_ms'):
                    predicted_ms = resp.predicted_execution_time_ms
                if len(res) > 2 and res[2]:
                    await self._handle_lighter_error(res[2], "create_limit")
                    raise ExchangeError(f"Lighter Order Error: {res[2]}")
            elif isinstance(res, dict):
                order_id = str(res.get('id') or res.get('order_id') or client_order_id)

            if order_id == "unknown":
                await asyncio.sleep(0.2)
                verified = await self._verify_order_created(symbol, order_id, client_order_id)
                if verified:
                    order_id = verified.id
                else:
                    logger.warning(
                        f"Lighter create_order verification failed for {symbol} (client_id={client_order_id})."
                    )

            return Order(
                id=order_id if order_id != "unknown" else client_order_id,
                symbol=symbol,
                type=type,
                side=side,
                amount=amount,
                price=price,
                status=OrderStatus.OPEN,
                client_order_id=client_order_id,
                timestamp=int(predicted_ms) if predicted_ms else None,
                raw=res
            )

        except Exception as e:
            await self._handle_lighter_error(e, "create_order")
            raise ExchangeError(f"Lighter Order Failed: {e}")

    async def fetch_balance(self) -> Balance:
        if not self.client or self.account_index < 0:
            return Balance(currency='USDT', total=0.0, free=0.0, used=0.0)
            
        try:
            data = None
            try:
                from lighter.api import account_api
                if self.client and self.client.api_client:
                    api_client = self.client.api_client
                else:
                    from lighter.api_client import ApiClient
                    api_client = ApiClient(self.config_obj)
                if self.auth_token:
                    api_client.default_headers["Authorization"] = self.auth_token
                api = account_api.AccountApi(api_client)
                import inspect
                if inspect.iscoroutinefunction(api.account):
                    res = await api.account(by="index", value=str(self.account_index))
                else:
                    res = await asyncio.to_thread(api.account, by="index", value=str(self.account_index))
                if hasattr(res, "to_dict"):
                    data = res.to_dict()
                elif isinstance(res, dict):
                    data = res
            except Exception:
                data = None

            if data is None:
                import aiohttp
                # REST Call to get Account (Same as legacy)
                url = f"{self.config_obj.host}/api/v1/account?by=index&value={self.account_index}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers={"accept": "application/json"}, timeout=10) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
            
            # Note: AbstractExchange expects Dict[str, Balance] or Single Balance?
            # AbstractExchange defined fetch_balance() -> Balance (Single or Dict?)
            # Usually fetch_balance returns a Dict of Currencies -> Balance.
            # But here prompt says "fetch_balance(self) -> Balance". 
            # If so, it returns Total Account Value? 
            # Models.py definition: Balance(total, free, used).
            # I will return the Account Collateral (USDC/USDT) as the main Balance.
            
            if data and data.get('accounts'):
                acc = data['accounts'][0]
                total = float(acc.get('collateral', 0)) 
                free = float(acc.get('available_balance', 0))
                used = total - free
                return Balance(currency='USDT', total=total, free=free, used=used)
                
            return Balance(currency='USDT', total=0.0, free=0.0, used=0.0)
        except Exception as e:
            logger.error(f"Fetch Balance Failed: {e}")
            return Balance(currency='USDT', total=0.0, free=0.0, used=0.0)
    
    async def fetch_positions(self) -> List[Position]:
        if not self.client or self.account_index < 0: return []
        
        try:
            data = None
            try:
                from lighter.api import account_api
                if self.client and self.client.api_client:
                    api_client = self.client.api_client
                else:
                    from lighter.api_client import ApiClient
                    api_client = ApiClient(self.config_obj)
                if self.auth_token:
                    api_client.default_headers["Authorization"] = self.auth_token
                api = account_api.AccountApi(api_client)
                import inspect
                if inspect.iscoroutinefunction(api.account):
                    res = await api.account(by="index", value=str(self.account_index))
                else:
                    res = await asyncio.to_thread(api.account, by="index", value=str(self.account_index))
                if hasattr(res, "to_dict"):
                    data = res.to_dict()
                elif isinstance(res, dict):
                    data = res
            except Exception:
                data = None

            if data is None:
                import aiohttp
                url = f"{self.config_obj.host}/api/v1/account?by=index&value={self.account_index}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers={"accept": "application/json"}, timeout=10) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
            
            positions = []
            if data and data.get('accounts'):
                acc = data['accounts'][0]
                for p in acc.get('positions', []):
                    if hasattr(p, "to_dict"):
                        p = p.to_dict()
                    # Check size
                    size = float(p.get('position', 0) or 0)
                    sign = int(p.get('sign', 1) or 1)
                    signed = size * (1 if sign >= 0 else -1)
                    if signed == 0:
                        continue
                    
                    mid = p.get('market_id') or p.get('market_index')
                    symbol = p.get('symbol')
                    mapped_symbol = self.markets_map.get(mid) if mid is not None else None
                    if mapped_symbol:
                        symbol = mapped_symbol
                    if symbol and "-" not in symbol:
                        candidate = f"{symbol}-USDT"
                        if candidate in self.markets_map or candidate in self.markets:
                            symbol = candidate
                    if not symbol:
                        continue
                    
                    # Parse Side (Lighter might use 'sign' or +/- size)
                    # Legacy used: sign = int(p.get('sign', 1))
                    
                    # Entry Price
                    entry_price = float(p.get('avg_entry_price', 0))
                    
                    # Leverage Calculation (Heuristic from Legacy)
                    leverage = 1.0
                    imf = float(p.get('initial_margin_fraction', 0))
                    if imf > 0:
                        leverage = 100.0 / (imf * 100) # Assuming imf is 0.1 for 10%? 
                        # Legacy: 100 / float(imf_str). If imf_str="10", then 10x.
                        # Wait, Margin Fraction 0.1 = 10%. 1/0.1 = 10.
                        # If Legacy used 100 / imf, then imf was likely Percentage (e.g. 10).
                        # Let's use simple logic: 1 / imf if imf < 1?
                        # Or just use Legacy logic: 100 / float(imf) (Assuming imf is int/float like 10.0)
                        leverage = 100.0 / imf 
                    
                    positions.append(Position(
                        symbol=symbol,
                        side=OrderSide.BUY if signed > 0 else OrderSide.SELL,
                        amount=abs(signed), # Clean size
                        leverage=leverage,
                        entry_price=entry_price,
                        unrealized_pnl=float(p.get('unrealized_pnl', 0))
                    ))
            
            return positions
        except Exception as e:
            logger.error(f"Fetch Positions Failed: {e}")
            return []

    async def cancel_order(self, id: str, symbol: Optional[str] = None) -> bool:
        try:
            mid = 0
            if symbol:
                mid = self.markets_map.get(symbol, 0)
                
            # cancel_order(order_id, market_index)
            res = await self.client.cancel_order(market_index=mid, order_index=int(id))
            if isinstance(res, tuple):
                _, resp, err = res
                if err:
                    return False
                if resp is not None and hasattr(resp, "code"):
                    try:
                        return int(resp.code) == 200
                    except Exception:
                        pass
            return True
        except Exception as e:
            return False

    def _parse_order_dict(self, d: Dict[str, Any]) -> Order:
        mid = d.get("market_id")
        if mid is None:
            mid = d.get("market_index")
        sym = self.markets_map.get(mid) or "Unknown"

        side = OrderSide.BUY
        if "is_ask" in d:
            side = OrderSide.SELL if d.get("is_ask") else OrderSide.BUY
        else:
            side_raw = str(d.get("side") or "").lower()
            if side_raw in ("sell", "ask"):
                side = OrderSide.SELL

        size_decimals = 6
        price_decimals = 2
        if sym in self.markets and getattr(self.markets[sym], "raw", None):
            raw = self.markets[sym].raw
            try:
                size_decimals = int(raw.get("supported_size_decimals", size_decimals))
            except (TypeError, ValueError):
                pass
            try:
                price_decimals = int(raw.get("supported_price_decimals", price_decimals))
            except (TypeError, ValueError):
                pass

        amount_raw = d.get("remaining_base_amount")
        if amount_raw is None:
            amount_raw = d.get("initial_base_amount")
        if amount_raw is None:
            amount_raw = d.get("base_size")
        price_raw = d.get("price")
        if price_raw is None:
            price_raw = d.get("base_price")

        amount = float(amount_raw or 0) / (10 ** size_decimals)
        price = float(price_raw or 0) / (10 ** price_decimals)

        order_index = d.get("order_index") or d.get("client_order_index") or d.get("order_id") or d.get("id")
        client_order_id = d.get("client_order_id") or d.get("client_order_index") or order_index

        status_raw = str(d.get("status") or "").lower()
        status = OrderStatus.OPEN
        if status_raw:
            if "cancel" in status_raw:
                status = OrderStatus.CANCELED
            elif status_raw in ("filled", "closed"):
                status = OrderStatus.CLOSED
            elif status_raw in ("open", "pending", "in-progress"):
                status = OrderStatus.OPEN

        otype = d.get("type") or "limit"
        otype = str(otype).lower()
        if otype not in ("limit", "market"):
            otype = "limit"

        return Order(
            id=str(order_index) if order_index is not None else "unknown",
            symbol=sym,
            type=OrderType(otype),
            side=side,
            amount=amount,
            price=price,
            status=status,
            client_order_id=str(client_order_id) if client_order_id is not None else None,
            raw=d,
        )


    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Order]:
        if not self.client or self.account_index < 0: return []
        
        try:
            from lighter.api import order_api
            if self.client and self.client.api_client:
                api_client = self.client.api_client
            else:
                from lighter.api_client import ApiClient
                api_client = ApiClient(self.config_obj)
            if self.auth_token:
                api_client.default_headers["Authorization"] = self.auth_token
            api = order_api.OrderApi(api_client)
            
            # Run in thread
            # account_active_orders(account_index=..., market_id=...)
            market_id = None
            if symbol:
                market_id = self.markets_map.get(symbol)
            if market_id is None:
                market_id = 0
            # account_active_orders may be async in SDK
            import inspect
            if inspect.iscoroutinefunction(api.account_active_orders):
                res = await api.account_active_orders(
                    account_index=self.account_index,
                    market_id=int(market_id),
                )
            else:
                res = await asyncio.to_thread(
                    api.account_active_orders,
                    account_index=self.account_index,
                    market_id=int(market_id),
                )
            
            # Parse response
            # res might be a list or object with 'orders' property?
            # Based on probe logic for other endpoints, might need 'to_dict'.
            target_list = []
            if hasattr(res, 'orders'): target_list = res.orders
            elif hasattr(res, 'to_dict'): 
                d = res.to_dict()
                target_list = d.get('orders', [])
            elif isinstance(res, list): target_list = res
            elif isinstance(res, dict): target_list = res.get('orders', [])
            
            orders = []
            for item in target_list:
                d = item
                if hasattr(item, 'to_dict'):
                    d = item.to_dict()
                
                # Filter by symbol if needed
                # item usually has 'market_id'.
                mid = d.get('market_id')
                if mid is None:
                    mid = d.get("market_index")
                sym = self.markets_map.get(mid)
                
                if symbol and sym != symbol: continue
                orders.append(self._parse_order_dict(d))
            return orders
        except Exception as e:
            logger.error(f"Fetch Open Orders Failed: {e}")
            return []

    async def _verify_order_created(self, symbol: str, order_id: Optional[str], client_order_id: Optional[str]) -> Optional[Order]:
        try:
            open_orders = await self.fetch_open_orders(symbol)
        except Exception as e:
            logger.warning(f"Lighter fetch_open_orders verify failed: {e}")
            return None
        for o in open_orders:
            if order_id and str(o.id) == str(order_id):
                return o
            if client_order_id and (
                str(o.client_order_id) == str(client_order_id) or str(o.id) == str(client_order_id)
            ):
                return o
        target_id = order_id or client_order_id
        if target_id:
            try:
                return await self.fetch_order(str(target_id), symbol)
            except Exception as e:
                logger.warning(f"Lighter fetch_order verify failed ({symbol} {target_id}): {e}")
        return None
    
    async def fetch_order(self, id: str, symbol: Optional[str] = None) -> Order:
        orders = await self.fetch_open_orders(symbol)
        for o in orders:
            if o.id == str(id) or (o.client_order_id and o.client_order_id == str(id)):
                return o
        
        # Check inactive orders for fills/cancels.
        try:
            from lighter.api import order_api
            if self.client and self.client.api_client:
                api_client = self.client.api_client
            else:
                from lighter.api_client import ApiClient
                api_client = ApiClient(self.config_obj)
            if self.auth_token:
                api_client.default_headers["Authorization"] = self.auth_token
            api = order_api.OrderApi(api_client)

            market_id = None
            if symbol:
                market_id = self.markets_map.get(symbol)
            params = {
                "account_index": self.account_index,
                "limit": 50,
            }
            if market_id is not None:
                params["market_id"] = int(market_id)

            import inspect
            if inspect.iscoroutinefunction(api.account_inactive_orders):
                res = await api.account_inactive_orders(**params)
            else:
                res = await asyncio.to_thread(api.account_inactive_orders, **params)

            target_list = []
            if hasattr(res, 'orders'):
                target_list = res.orders
            elif hasattr(res, 'to_dict'):
                d = res.to_dict()
                target_list = d.get('orders', [])
            elif isinstance(res, list):
                target_list = res
            elif isinstance(res, dict):
                target_list = res.get('orders', [])

            for item in target_list:
                d = item.to_dict() if hasattr(item, "to_dict") else item
                order_obj = self._parse_order_dict(d)
                if order_obj.id == str(id) or (order_obj.client_order_id and order_obj.client_order_id == str(id)):
                    return order_obj
        except Exception as e:
            logger.warning(f"Fetch Order {id} inactive check failed: {e}")

        raise OrderNotFound("Order not found in active or inactive orders")

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        remaining = self._cooldown_remaining()
        if remaining > 0:
            logger.warning(
                "Lighter cooldown active (%.0fs). Skip set_leverage for %s.",
                remaining,
                symbol,
            )
            return False
        try:
            # Map symbol to ID
            mid = self.markets_map.get(symbol)
            
            # Fallback (Legacy)
            if mid is None:
                 if 'ETH' in symbol: mid = 0
                 elif 'BTC' in symbol: mid = 1
            
            if mid is None:
                raise ExchangeError(f"Market for {symbol} not found")
                
            # SDK may expose sync or async update_leverage with different signatures
            import inspect
            sig = inspect.signature(self.client.update_leverage)
            params = sig.parameters
            kwargs = {}
            args = []

            if "market_index" in params:
                kwargs["market_index"] = int(mid)
            elif "market_id" in params:
                kwargs["market_id"] = int(mid)
            elif "market" in params:
                kwargs["market"] = int(mid)

            if "margin_mode" in params:
                margin_mode = getattr(self.client, "CROSS_MARGIN_MODE", 0)
                kwargs["margin_mode"] = margin_mode

            if "leverage" in params:
                kwargs["leverage"] = int(leverage)
            elif "new_leverage" in params:
                kwargs["new_leverage"] = int(leverage)

            if not kwargs:
                # Fallback to positional order: (market_index, margin_mode, leverage)
                margin_mode = getattr(self.client, "CROSS_MARGIN_MODE", 0)
                args = [int(mid), int(margin_mode), int(leverage)]

            if asyncio.iscoroutinefunction(self.client.update_leverage):
                if kwargs:
                    await self.client.update_leverage(**kwargs)
                else:
                    await self.client.update_leverage(*args)
            else:
                if kwargs:
                    await asyncio.to_thread(self.client.update_leverage, **kwargs)
                else:
                    await asyncio.to_thread(self.client.update_leverage, *args)
            return True
        except Exception as e:
            await self._handle_lighter_error(e, "set_leverage")
            logger.error(f"Set Leverage Failed: {e}")
            return False

    async def close(self):
        # Close WS client
        try:
            if self._ws_client and self._ws_client.open:
                await self._ws_client.close()
            self._ws_client = None
        except Exception:
            pass
        # Cancel WS loop task
        try:
            if self._ws_task:
                self._ws_task.cancel()
                self._ws_task = None
        except Exception:
            pass
        # Close SignerClient/ApiClient sessions
        try:
            if self.client and hasattr(self.client, "close"):
                if asyncio.iscoroutinefunction(self.client.close):
                    await self.client.close()
                else:
                    await asyncio.to_thread(self.client.close)
        except Exception:
            pass
        try:
            if self.rest_client and hasattr(self.rest_client, "close"):
                if asyncio.iscoroutinefunction(self.rest_client.close):
                    await self.rest_client.close()
                else:
                    await asyncio.to_thread(self.rest_client.close)
        except Exception:
            pass

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        if symbol in self._funding_cache:
            item = self._funding_cache[symbol]
            logger.debug(f"get_funding_rate({symbol}) HIT: {item}")
            return item
        
        # Debug why we miss
        # if self._ws_client:
        logger.debug(f"get_funding_rate({symbol}) MISS. Cache keys: {list(self._funding_cache.keys())}")
        
        return FundingRate(symbol, 0.0, 0)
