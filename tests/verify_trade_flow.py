
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
from src.shared_crypto_lib.models import OrderType, OrderSide, OrderStatus

# Force config to output to stdout even if root logger captured
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True,
    stream=sys.stdout
)
logger = logging.getLogger("TradeVerify")

async def test_exchange(name, exchange, symbol, min_qty):
    logger.info(f"\n[{name}] Starting Live Trade Test on {symbol}...")
    
    try:
        # 1. Market Data
        logger.info(f"[{name}] Fetching Ticker...")
        ticker = await exchange.fetch_ticker(symbol)
        price = ticker.last or ticker.ask or 0
        if price <= 0:
            logger.error(f"[{name}] Invalid Price: {price}. Skipping.")
            return

        logger.info(f"[{name}] Price: {price}")
        
        # 2. Error Handling Test
        logger.info(f"[{name}] Testing Invalid Order (Size 0)...")
        try:
            await exchange.create_order(symbol, OrderType.LIMIT, OrderSide.BUY, 0, price * 0.5)
            logger.error(f"[{name}] Failed: Zero size order did not raise exception.")
        except Exception as e:
            logger.info(f"[{name}] Success: Caught expected error: {e}")

        # 3. Limit Order Test (Deep OTM)
        buy_price = price * 0.5 # 50% below
        logger.info(f"[{name}] Placing Limit Buy at {buy_price}...")
        
        limit_order = await exchange.create_order(symbol, OrderType.LIMIT, OrderSide.BUY, min_qty, buy_price)
        logger.info(f"[{name}] Limit Order Placed: ID={limit_order.id}, Status={limit_order.status}")
        
        # Check Open Orders
        open_orders = await exchange.fetch_open_orders(symbol)
        found = any(o.id == limit_order.id for o in open_orders)
        if found:
            logger.info(f"[{name}] Order found in open_orders.")
        else:
            logger.warning(f"[{name}] Order NOT found in open_orders.")
            
        # Cancel
        logger.info(f"[{name}] Canceling Order {limit_order.id}...")
        canceled = await exchange.cancel_order(limit_order.id, symbol)
        if canceled:
            logger.info(f"[{name}] Cancel Request Sent.")
        else:
            logger.warning(f"[{name}] Cancel Request Failed.")
            
        # Verify Cancel (Wait a bit)
        await asyncio.sleep(2)
        open_orders_after = await exchange.fetch_open_orders(symbol)
        found_after = any(o.id == limit_order.id for o in open_orders_after)
        if not found_after:
            logger.info(f"[{name}] Order successfully removed from open_orders.")
        else:
            logger.warning(f"[{name}] Order STILL present after cancel.")

        # 4. Market Order Test (Live Entry/Exit)
        logger.info(f"[{name}] --- Starting Market Order Test (Real Trade) ---")
        
        # Check Balance/Position before
        positions_before = await exchange.fetch_positions()
        pos_before = next((p for p in positions_before if p.symbol == symbol), None)
        qty_before = pos_before.amount if pos_before else 0
        
        # Buy
        logger.info(f"[{name}] Market Buying {min_qty}...")
        try:
            market_buy = await exchange.create_order(symbol, OrderType.MARKET, OrderSide.BUY, min_qty)
            logger.info(f"[{name}] Market Buy Placed: {market_buy.id}")
            await asyncio.sleep(2) # Wait for fill
            
            # Check Position
            positions_mid = await exchange.fetch_positions()
            pos_mid = next((p for p in positions_mid if p.symbol == symbol), None)
            qty_mid = pos_mid.amount if pos_mid else 0
            
            logger.info(f"[{name}] Position Change: {qty_before} -> {qty_mid}")
            
            if qty_mid > qty_before:
                logger.info(f"[{name}] Position Increased! Entry Successful.")
                
                # Sell (Close)
                logger.info(f"[{name}] Market Selling {min_qty} (Closing)...")
                market_sell = await exchange.create_order(symbol, OrderType.MARKET, OrderSide.SELL, min_qty)
                logger.info(f"[{name}] Market Sell Placed: {market_sell.id}")
                await asyncio.sleep(2)
                
                # Check Position
                positions_after = await exchange.fetch_positions()
                pos_after = next((p for p in positions_after if p.symbol == symbol), None)
                qty_after = pos_after.amount if pos_after else 0
                
                logger.info(f"[{name}] Position Change: {qty_mid} -> {qty_after}")
                
                if qty_after < qty_mid:
                    logger.info(f"[{name}] Position Decreased! Exit Successful.")
                else:
                    logger.error(f"[{name}] Exit Failed or Pending.")
            else:
                logger.error(f"[{name}] Entry Failed or Pending.")
                
        except Exception as e:
            logger.error(f"[{name}] Market Order Test Failed: {e}")

    except Exception as e:
        logger.error(f"[{name}] Critical Error: {e}", exc_info=True)

async def main():
    # Initialize
    grvt = GrvtExchange({
        'api_key': Config.GRVT_API_KEY, 
        'private_key': Config.GRVT_PRIVATE_KEY, 
        'subaccount_id': Config.GRVT_TRADING_ACCOUNT_ID, 
        'env': Config.GRVT_ENV
    })
    lighter = LighterExchange({
        'wallet_address': Config.LIGHTER_WALLET_ADDRESS,
        'private_key': Config.LIGHTER_PRIVATE_KEY,
        'public_key': Config.LIGHTER_PUBLIC_KEY,
        'api_key_index': Config.LIGHTER_API_KEY_INDEX,
        'env': Config.LIGHTER_ENV
    })
    
    variational = None
    if Config.VARIATIONAL_WALLET_ADDRESS:
        variational = VariationalExchange({
            'wallet_address': Config.VARIATIONAL_WALLET_ADDRESS,
            'vr_token': Config.VARIATIONAL_VR_TOKEN
        })

    await grvt.initialize()
    await lighter.initialize()
    if variational: 
        try:
             await variational.initialize()
        except: pass

    # Run Tests
    # Symbols: GRVT="BTC_USDT_Perp", Lighter="WBTC-USDT", Variational="BTC"
    # Min Qty: GRVT=0.001? (Check market rules), Lighter=0.0001?, Variational=0.0001?
    
    # Use ETH for cheaper tests check
    # GRVT: ETH_USDT_Perp
    # Lighter: ETH-USDT
    # Variational: ETH
    
    await test_exchange("GRVT", grvt, "ETH_USDT_Perp", 0.01)
    await test_exchange("Lighter", lighter, "ETH-USDT", 0.01)
    
    if variational:
        await test_exchange("Variational", variational, "ETH", 0.01)

if __name__ == "__main__":
    asyncio.run(main())
