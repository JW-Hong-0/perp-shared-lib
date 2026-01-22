
import asyncio
import logging
import sys
import os
import inspect

# Path setup
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from src.GRVT_Lighter_Bot.config import Config

# Lighter Imports
try:
    import lighter
    from lighter.configuration import Configuration
    from lighter.api_client import ApiClient
    from lighter.api import order_api, funding_api
except ImportError as e:
    print(f"Lighter Import Error: {e}")
    sys.exit(1)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LighterProbe")

async def probe_lighter():
    logger.info("Starting Lighter Probe...")
    
    # 1. Config
    conf = Configuration()
    host = "https://mainnet.zklighter.elliot.ai" # User requested Mainnet
    conf.host = host
    
    # Auth needed? Read APIs usually public.
    
    api_client = ApiClient(conf)
    
    # 2. Funding API
    f_api = funding_api.FundingApi(api_client)
    try:
        logger.info("--- Probing Funding Rates ---")
        rates = await f_api.funding_rates()
        # response might be object or list
        logger.info(f"Funding Rates Type: {type(rates)}")
        if hasattr(rates, 'to_dict'):
            logger.info(f"Funding Rates Data: {rates.to_dict()}")
        else:
            logger.info(f"Funding Rates Data: {rates}")
    except Exception as e:
        logger.error(f"Funding Probe Failed: {e}")

    # 3. Order Book API
    o_api = order_api.OrderApi(api_client)
    try:
        logger.info("--- Probing Order Book Details ---")
        # Need market_id. Try 0 (ETH-USDC) and 1 (WBTC-USDC) usually on Mainnet?
        # Need to confirm IDs from Asset Details first
        pass 
            
    except Exception as e:
        logger.error(f"OrderBook Probe Failed: {e}")
        
    # 4. Market Info / Asset Details
    market_ids = []
    try:
        logger.info("--- Probing Asset Details ---")
        assets = await o_api.asset_details()
        logger.info(f"Assets: {assets}")
        # Try to extract IDs
    except Exception as e:
        logger.error(f"Asset Probe Failed: {e}")

    # 5. WebSocket Probe
    import websockets
    import json
    # Use config stream (Original Bot used stream)
    ws_url = "wss://mainnet.zklighter.elliot.ai/stream"
    
    try:
        logger.info(f"--- Probing WebSocket {ws_url} ---")
        async with websockets.connect(ws_url) as ws:
            # Subscribe to OrderBook (0)
            msg1 = json.dumps({"type": "subscribe", "channel": "order_book/0"})
            msg2 = json.dumps({"type": "subscribe", "channel": "market_stats/0"})
            
            await ws.send(msg1)
            await ws.send(msg2)
            logger.info(f"Sent Subscribe: {msg1}")
            
            for _ in range(5):
                res = await asyncio.wait_for(ws.recv(), timeout=5.0)
                logger.info(f"WS Msg: {res}")
                
    except Exception as e:
        logger.error(f"WS Probe Failed: {e}")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    loop.run_until_complete(probe_lighter())
