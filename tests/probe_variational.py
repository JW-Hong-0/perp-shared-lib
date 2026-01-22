
import asyncio
import logging
import sys
import os

# Path setup
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from src.GRVT_Lighter_Bot.config import Config

# Variational Imports
try:
    from src.GRVT_Lighter_Bot.variational_sdk.variational import VariationalExchange
except ImportError as e:
    print(f"Variational Import Error: {e}")
    sys.exit(1)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VariationalProbe")

async def probe_variational():
    logger.info("Starting Variational Probe...")
    
    # 1. Initialize
    # Assume Config has Testnet keys or Mainnet? User said "Mainnet" for Lighter, 
    # but Variational is likely Testnet only? 
    # I will use Config values.
    
    if not Config.VARIATIONAL_WALLET_ADDRESS:
        logger.error("No Variational Config found.")
        return

    try:
        ex = VariationalExchange(
            evm_wallet_address=Config.VARIATIONAL_WALLET_ADDRESS,
            session_cookies={"vr-token": Config.VARIATIONAL_VR_TOKEN}
        )
        await ex.initialize()
        logger.info("Variational Initialized.")
        
        # 2. Probe Positions
        logger.info("--- Probing Positions ---")
        pos = await ex._fetch_positions_all()
        logger.info(f"Positions Type: {type(pos)}")
        logger.info(f"Positions Data: {pos}")
        
        # 3. Probe Order (Safe?)
        # Only if positions exist? Or small order.
        # User requested Limit Order test.
        # I'll try to fetch price first.
        price = await ex.fetch_price("ETH")
        logger.info(f"ETH Price: {price}")
        
        if price and price > 0:
            logger.info("--- Probing Order (Limit Buy far below) ---")
            safe_price = price * 0.5
            # Create Order
            # amount=0.01, price=safe_price, side='buy', type='limit'
            res = await ex.create_order(
                symbol="ETH",
                side='buy',
                amount=0.01,
                price=safe_price,
                order_type='limit'
            )
            logger.info(f"Order Result Type: {type(res)}")
            logger.info(f"Order Result Data: {res}")
            
            # Cancel if possible?
            # SDK doesn't always expose cancel by ID easily?
            
    except Exception as e:
        logger.error(f"Variational Probe Failed: {e}")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    loop.run_until_complete(probe_variational())
