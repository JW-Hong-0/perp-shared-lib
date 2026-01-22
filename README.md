# Perp Shared Lib

Shared exchange abstractions and adapters for perpetual DEX/CEX integrations.

## Usage

Add this repo to your bot project (submodule recommended):

```
libs/shared_crypto_lib
```

Then add `libs/` to `PYTHONPATH` or `sys.path`.

## Exchanges

- GRVT
- Lighter
- Variational
- HyENA (Based/Hyperliquid SDK, builder code required)

### HyENA Config (example)

```
{
  "private_key": "...",
  "wallet_address": "...",  # optional
  "dex_id": "hyna",
  "builder_address": "0x1924b8561eeF20e70Ede628A296175D358BE80e5",
  "builder_fee": 0,
  "approve_builder_fee": false,
  "builder_max_fee_rate": "0",
  "use_symbol_prefix": true,
  "slippage": 0.05
}
```

HyENA 심볼은 기본적으로 `hyna:COIN` 형식을 사용한다.

## Dependencies

Install from your bot's environment using the requirements below:

```
pip install -r requirements.txt
```
