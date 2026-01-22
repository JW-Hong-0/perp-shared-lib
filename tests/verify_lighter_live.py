import asyncio
import logging
import sys
import os

# Adjust path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../')))

from src.shared_crypto_lib.exchanges.lighter import LighterExchange
from src.shared_crypto_lib.models import OrderType, OrderSide
from src.GRVT_Lighter_Bot.config import Config

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', force=True, stream=sys.stdout)
logger = logging.getLogger("LighterLiveVerify")

async def verify_lighter():
    logger.info("Initializing Lighter Exchange (Mainnet Checks)...")
    
    # Init
    exchange = LighterExchange({
        'wallet_address': Config.LIGHTER_WALLET_ADDRESS,
        'private_key': Config.LIGHTER_PRIVATE_KEY,
        'public_key': Config.LIGHTER_PUBLIC_KEY,
        'api_key_index': Config.LIGHTER_API_KEY_INDEX,
        'env': Config.LIGHTER_ENV
    })
    
    await exchange.initialize()
    logger.info("Market Load Complete.")
    
    symbol = "ETH-USDT"
    logger.info(f"Checking {symbol}...")
    
    # 1. Fetch Ticker
    ticker = await exchange.fetch_ticker(symbol)
    logger.info(f"Ticker: {ticker}")
    if ticker.last == 0:
        logger.warning("Ticker Last is 0, using fallback price 2000 for test order logic.")
        price = 2000.0
    else:
        price = ticker.last

    # 2. Place Limit Order (Buy at 50% price to avoid fill)
    target_price = price * 0.5
    qty = 0.001 # Min size
    
    logger.info(f"Placing LIMIT BUY Order: {symbol} @ {target_price} Qty={qty}")
    
    try:
        order = await exchange.create_order(
            symbol=symbol,
            type=OrderType.LIMIT,
            side=OrderSide.BUY,
            amount=qty,
            price=target_price
        )
        logger.info(f"✅ Order Placed Successfully! ID: {order.id}")
        logger.info(f"Order Details: {order}")
        
        # 3. Cancel Order
        logger.info("Canceling Order...")
        await asyncio.sleep(1)
        res = await exchange.cancel_order(order.id, symbol)
        
        if res:
             logger.info("✅ Order Canceled Successfully.")
        else:
             logger.warning("Cancel returned False/None (Check Status).")
             
    except Exception as e:
        logger.error(f"❌ Trade Verification Failed: {e}")

if __name__ == "__main__":
    asyncio.run(verify_lighter())
