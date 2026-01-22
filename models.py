from dataclasses import dataclass, field
from typing import Optional, List, Dict
from enum import Enum

class OrderSide(Enum):
    BUY = 'buy'
    SELL = 'sell'

class OrderType(Enum):
    MARKET = 'market'
    LIMIT = 'limit'

class OrderStatus(Enum):
    OPEN = 'open'
    CLOSED = 'closed'
    CANCELED = 'canceled'
    EXPIRED = 'expired'
    REJECTED = 'rejected'

@dataclass
class MarketInfo:
    """Metadata about a trading pair."""
    symbol: str
    base: str
    quote: str
    min_qty: float
    max_qty: Optional[float] = None
    qty_step: float = 0.0
    price_tick: float = 0.0
    min_notional: Optional[float] = None # Min USDT value
    max_leverage: Optional[float] = None
    active: bool = True

@dataclass
class Ticker:
    """Snapshot of market price/book."""
    symbol: str
    bid: float
    ask: float
    last: float
    timestamp: int # ms
    bid_volume: Optional[float] = None
    ask_volume: Optional[float] = None

@dataclass
class Order:
    """Standardized Order Return."""
    id: str
    symbol: str
    type: OrderType
    side: OrderSide
    amount: float
    price: Optional[float] = None
    status: OrderStatus = OrderStatus.OPEN
    filled: float = 0.0
    remaining: float = 0.0
    cost: float = 0.0 # Executed value
    fee: Optional[float] = None
    client_order_id: Optional[str] = None
    timestamp: Optional[int] = None
    raw: Optional[dict] = None # Raw API response

@dataclass
class Position:
    """Futures Position Info."""
    symbol: str
    side: OrderSide # BUY (Long) or SELL (Short)
    amount: float # Usually positive, side determines direction. Or signed? 
                  # Convention: amount always positive, side determines. 
                  # BUT simpler: signed amount. 
                  # Let's stick to: amount >= 0, side = 'buy'/'sell'.
    entry_price: float
    unrealized_pnl: float
    leverage: float
    liquidation_price: Optional[float] = None
    collateral: Optional[float] = None
    mark_price: Optional[float] = None

@dataclass
class Balance:
    """Account Balance."""
    currency: str
    total: float
    free: float
    used: float

@dataclass
class FundingRate:
    """Funding Rate Info."""
    symbol: str
    rate: float # 0.0001 = 1bps
    next_funding_time: int # Timestamp ms
    predicted_rate: Optional[float] = None

