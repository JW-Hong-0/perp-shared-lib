
import asyncio
import logging
import sys
import os

# Path setup
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from src.GRVT_Lighter_Bot.config import Config
from src.shared_crypto_lib.exchanges.grvt import GrvtExchange
from src.shared_crypto_lib.exchanges.lighter import LighterExchange
from src.shared_crypto_lib.exchanges.variational import VariationalExchange

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("InterfaceVerify")

async def verify():
    logger.info("--- Starting Interface Verification ---")
    
    # 1. GRVT
    try:
        grvt = GrvtExchange({
            'api_key': Config.GRVT_API_KEY,
            'private_key': Config.GRVT_PRIVATE_KEY,
            'subaccount_id': Config.GRVT_TRADING_ACCOUNT_ID,
            'env': Config.GRVT_ENV
        })
        logger.info("Initializing GRVT...")
        await grvt.initialize()
        logger.info(f"GRVT Initialized. Markets: {len(grvt.markets)}")
    except Exception as e:
        logger.error(f"GRVT Init Failed: {e}")

    # 2. Lighter
    try:
        lighter = LighterExchange({
            'wallet_address': Config.LIGHTER_WALLET_ADDRESS,
            'private_key': Config.LIGHTER_PRIVATE_KEY,
            'public_key': Config.LIGHTER_PUBLIC_KEY,
            'api_key_index': Config.LIGHTER_API_KEY_INDEX,
            'env': Config.LIGHTER_ENV
        })
        logger.info("Initializing Lighter...")
        await lighter.initialize()
        logger.info(f"Lighter Initialized. Markets: {len(lighter.markets)}")
        
        # Check Ticker Fetch (REST fallback or wait for WS?)
        # Since initialize doesn't start WS loop automatically (design choice?), we call subscribe to start it?
        # BUT fetch_ticker should return empty Ticker if no data.
        t = await lighter.fetch_ticker("ETH-USDT")
        logger.info(f"Lighter Ticker Check: {t}")
        
    except Exception as e:
        logger.error(f"Lighter Init Failed: {e}")

    # 3. Variational (Might fail if auth blocks, but verify class works)
    try:
        # Check if keys exist
        if Config.VARIATIONAL_WALLET_ADDRESS:
            var = VariationalExchange({
                'wallet_address': Config.VARIATIONAL_WALLET_ADDRESS,
                'vr_token': Config.VARIATIONAL_VR_TOKEN
            })
            # Skip initialize to avoid blocking if token expired?
            # Or try catch.
            logger.info("Initializing Variational...")
            await var.initialize()
            logger.info("Variational Initialized.")
        else:
            logger.warning("Variational Keys missing in Config. Skipping.")
            
    except Exception as e:
        logger.error(f"Variational Init Failed: {e}")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    loop.run_until_complete(verify())
