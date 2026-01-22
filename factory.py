from enum import Enum
from typing import Dict, Any, Optional

# Import Implementations
# Note: Using relative imports
from .exchanges.grvt import GrvtExchange
from .exchanges.lighter import LighterExchange
from .exchanges.variational import VariationalExchange
from .base import AbstractExchange as ExchangeBase

class ExchangeType(Enum):
    GRVT = "GRVT"
    LIGHTER = "LIGHTER"
    VARIATIONAL = "VARIATIONAL"

class ExchangeFactory:
    @staticmethod
    def create_exchange(exchange_type: ExchangeType, config: Dict[str, Any] = None) -> ExchangeBase:
        """
        Factory method to create exchange instances.
        
        Args:
            exchange_type (ExchangeType): Type of exchange (GRVT, LIGHTER, VARIATIONAL)
            config (Dict): Configuration dictionary (credentials, etc.)
            
        Returns:
            ExchangeBase: Instance of the exchange
        """
        if config is None:
            config = {}
            
        if exchange_type == ExchangeType.GRVT:
            return GrvtExchange(config)
        elif exchange_type == ExchangeType.LIGHTER:
            return LighterExchange(config)
        elif exchange_type == ExchangeType.VARIATIONAL:
            return VariationalExchange(config)
        else:
            raise ValueError(f"Unknown exchange type: {exchange_type}")
