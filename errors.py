
class ExchangeError(Exception):
    """Base exception for all exchange related errors."""
    pass

class NetworkError(ExchangeError):
    """Raised when a network error occurs."""
    pass

class AuthenticationError(ExchangeError):
    """Raised when authentication fails."""
    pass

class PermissionDenied(ExchangeError):
    """Raised when API key does not have permission."""
    pass

class AccountSuspended(ExchangeError):
    """Raised when account is suspended/frozen."""
    pass

class InvalidOrder(ExchangeError):
    """Raised when order parameters are invalid."""
    pass

class OrderNotFound(ExchangeError):
    """Raised when attempting to fetch/cancel a non-existent order."""
    pass

class InsufficientFunds(ExchangeError):
    """Raised when balance is insufficient."""
    pass

class OrderImmediatelyFillable(InvalidOrder):
    """Raised for Post-Only orders that would fill immediately like a Taker."""
    pass

class RateLimitExceeded(ExchangeError):
    """Raised when API rate limit is exceeded."""
    pass

class BadSymbol(ExchangeError):
    """Raised when symbol is invalid or not supported."""
    pass

class CircuitBreakerTriggered(ExchangeError):
    """Raised when an operation is blocked due to internal risk controls."""
    pass
