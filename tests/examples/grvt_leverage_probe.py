import asyncio
import json
import os
from src.shared_crypto_lib.exchanges.grvt import GrvtExchange
from src.shared_crypto_lib.tests.examples._probe_utils import (
    load_env,
    now_tag,
    output_dir,
    to_plain,
    write_json,
    write_md,
)


def _render_block(title: str, payload: dict) -> list[str]:
    lines = [f"## {title}", ""]
    lines.append("```json")
    lines.append(json.dumps(payload, indent=2, ensure_ascii=True, default=str))
    lines.append("```")
    lines.append("")
    return lines


async def main():
    env_path = load_env()
    from src.GRVT_Lighter_Bot.config import Config

    symbol = os.getenv("GRVT_SYMBOL", "AVNT_USDT_Perp")
    leverage = os.getenv("GRVT_LEVERAGE", "5")
    margin_type = os.getenv("GRVT_MARGIN_TYPE", "CROSS")
    skip_initial = os.getenv("SKIP_SET_INITIAL_LEVERAGE", "0") == "1"
    skip_position = os.getenv("SKIP_SET_POSITION_CONFIG", "0") == "1"
    tag = now_tag()
    out_dir = output_dir()
    json_path = out_dir / f"grvt_leverage_probe_{tag}.json"
    md_path = out_dir / f"grvt_leverage_probe_{tag}.md"

    report = {
        "env": Config.GRVT_ENV,
        "symbol": symbol,
        "leverage": leverage,
        "margin_type": margin_type,
        "env_path": str(env_path),
        "skip_set_initial_leverage": skip_initial,
        "skip_set_position_config": skip_position,
    }
    md_lines = [
        "# GRVT Leverage Probe",
        "",
        f"- env: {Config.GRVT_ENV}",
        f"- symbol: {symbol}",
        f"- leverage: {leverage}",
        f"- margin_type: {margin_type}",
        f"- env_path: {env_path}",
        f"- output_json: {json_path}",
        "",
    ]

    print(f"GRVT_ENV={Config.GRVT_ENV} symbol={symbol} leverage={leverage} margin_type={margin_type}")

    grvt = GrvtExchange({
        "api_key": Config.GRVT_API_KEY,
        "private_key": Config.GRVT_PRIVATE_KEY,
        "subaccount_id": Config.GRVT_TRADING_ACCOUNT_ID,
        "env": Config.GRVT_ENV,
        "use_position_config": True,
        "position_margin_type": margin_type,
    })

    try:
        await grvt.initialize()
    except Exception as e:
        report["initialize_error"] = str(e)
        md_lines.append(f"Initialize error: {e}")
        write_json(json_path, report)
        write_md(md_path, md_lines)
        return

    base_url = "https://trades.grvt.io" if Config.GRVT_ENV != "TESTNET" else "https://trades.testnet.grvt.io"

    if not skip_initial:
        payload = {
            "sub_account_id": Config.GRVT_TRADING_ACCOUNT_ID,
            "instrument": symbol,
            "leverage": str(leverage),
        }
        resp = None
        try:
            resp = await asyncio.to_thread(
                grvt.client._auth_and_post,
                f"{base_url}/full/v1/set_initial_leverage",
                payload=payload,
            )
            print("set_initial_leverage response:", resp)
        except Exception as e:
            resp = {"error": str(e)}
            print(f"set_initial_leverage failed: {e}")
        report["set_initial_leverage"] = {
            "payload": payload,
            "response": to_plain(resp),
        }
        md_lines.extend(_render_block("set_initial_leverage", report["set_initial_leverage"]))

    if not skip_position:
        resp = None
        try:
            ok = await grvt.set_position_config(symbol, float(leverage), margin_type)
            resp = {
                "ok": ok,
                "variant": getattr(grvt, "_last_position_config_variant", None),
                "payload": getattr(grvt, "_last_position_config_payload", None),
                "response": getattr(grvt, "_last_position_config_response", None),
            }
            print("set_position_config ok:", ok)
        except Exception as e:
            resp = {"error": str(e)}
            print(f"set_position_config failed: {e}")
        report["set_position_config"] = to_plain(resp)
        md_lines.extend(_render_block("set_position_config", report["set_position_config"]))

    await grvt.close()
    write_json(json_path, report)
    write_md(md_path, md_lines)
    print(f"Wrote report: {json_path}")
    print(f"Wrote report: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
