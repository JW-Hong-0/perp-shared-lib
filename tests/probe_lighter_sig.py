import asyncio
import logging
import sys
import inspect
from src.GRVT_Lighter_Bot.config import Config

# Helper to inspect Light SDK
async def probe_sig():
    try:
        from lighter.modules import SignerClient
    except ImportError:
        try:
             import lighter
             SignerClient = lighter.SignerClient # If exposed at top level?
             # Or inspect lighter package structure
        except:
             print("Could not import SignerClient directly.")
             return

    print("Inspect AccountApi:")
    try:
        from lighter.api import account_api
        print([x for x in dir(account_api.AccountApi) if "order" in x.lower() or "limit" in x.lower()])
        
    except Exception as e:
        print(f"AccountApi Probe Failed: {e}")

if __name__ == "__main__":
    asyncio.run(probe_sig())
