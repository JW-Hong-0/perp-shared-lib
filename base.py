from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from .models import Ticker, Order, Position, Balance, MarketInfo, OrderType, OrderSide, FundingRate

class AbstractExchange(ABC):
    """
    Standardized Abstract Base Class for Crypto Exchanges.
    Follows a CCXT-like interface pattern.
    """
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.markets: Dict[str, MarketInfo] = {} # Key: symbol (Standardized, e.g. BTC/USDT:USDT)
        self.is_initialized = False

    @abstractmethod
    async def initialize(self):
        """Authenticate and load necessary market data."""
        pass

    @abstractmethod
    async def close(self):
        """Cleanup resources (sessions, websockets)."""
        pass

    # --- Market Data ---

    @abstractmethod
    async def load_markets(self) -> Dict[str, MarketInfo]:
        """Fetch and parse market rules (min cost, precision) for all symbols."""
        pass

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> Ticker:
        """Fetch current price/book snapshot."""
        pass

    @abstractmethod
    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        """Fetch current and predicted funding rate."""
        pass

    async def fetch_order_book(self, symbol: str, limit: int = 10) -> Dict:
        """Fetch order book (bids/asks). Optional."""
        raise NotImplementedError("fetch_order_book not implemented")

    # --- WebSocket / Real-time ---
    
    @abstractmethod
    async def subscribe_ticker(self, symbol: str, callback):
        """
        Subscribe to real-time ticker updates.
        callback(ticker: Ticker)
        """
        pass

    async def subscribe_order_book(self, symbol: str, callback):
        """
        Subscribe to real-time order book updates.
        callback(book: Dict)
        """
        raise NotImplementedError("subscribe_order_book not implemented")

    # --- Account ---

    @abstractmethod
    async def fetch_balance(self) -> Dict[str, Balance]:
        """Fetch account balances key by currency."""
        pass

    @abstractmethod
    async def fetch_positions(self) -> List[Position]:
        """Fetch all open positions."""
        pass
    
    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a specific market."""
        pass

    # --- Trading ---

    @abstractmethod
    async def create_order(
        self, 
        symbol: str, 
        type: OrderType, 
        side: OrderSide, 
        amount: float, 
        price: Optional[float] = None, 
        params: Dict = {}
    ) -> Order:
        """
        Create a new order.
        type: MARKET or LIMIT
        side: BUY or SELL
        amount: Quantity in Base Asset (usually)
        price: Limit Price (required for LIMIT)
        params: Extra params (e.g. 'postOnly': True)
        """
        pass

    @abstractmethod
    async def cancel_order(self, id: str, symbol: Optional[str] = None) -> bool:
        """Cancel an order by ID."""
        pass

    @abstractmethod
    async def fetch_order(self, id: str, symbol: Optional[str] = None) -> Order:
        """Fetch status of a specific order."""
        pass

    @abstractmethod
    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """Fetch all open orders."""
        pass

    # --- Utils ---
    
    def get_market(self, symbol: str) -> MarketInfo:
        """Helper to get market info from cache."""
        if symbol not in self.markets:
            raise ValueError(f"Market {symbol} not loaded or invalid.")
        return self.markets[symbol]
