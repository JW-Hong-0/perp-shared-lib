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


def _split_symbols(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


async def main() -> None:
    env_path = load_env()
    from src.GRVT_Lighter_Bot.config import Config

    default_symbols = "AVNT_USDT_Perp,IP_USDT_Perp,BERA_USDT_Perp,RESOLV_USDT_Perp"
    symbols = _split_symbols(os.getenv("GRVT_SYMBOLS", default_symbols))
    leverage = os.getenv("GRVT_LEVERAGE", "20")
    leverage_snapshot_endpoint = os.getenv(
        "GRVT_LEVERAGE_SNAPSHOT_ENDPOINT",
        f"{'https://trades.grvt.io' if Config.GRVT_ENV != 'TESTNET' else 'https://trades.testnet.grvt.io'}/lite/v1/get_all_initial_leverage",
    )

    tag = now_tag()
    out_dir = output_dir()
    json_path = out_dir / f"grvt_set_position_config_{tag}.json"
    md_path = out_dir / f"grvt_set_position_config_{tag}.md"

    report = {
        "env": Config.GRVT_ENV,
        "symbols": symbols,
        "leverage": leverage,
        "env_path": str(env_path),
        "leverage_snapshot_endpoint": leverage_snapshot_endpoint,
        "results": [],
    }
    md_lines = [
        "# GRVT set_position_config Probe",
        "",
        f"- env: {Config.GRVT_ENV}",
        f"- symbols: {symbols}",
        f"- leverage: {leverage}",
        f"- env_path: {env_path}",
        f"- output_json: {json_path}",
        f"- leverage_snapshot_endpoint: {leverage_snapshot_endpoint}",
        "",
    ]

    print(f"GRVT_ENV={Config.GRVT_ENV} symbols={symbols} leverage={leverage}")

    grvt = GrvtExchange({
        "api_key": Config.GRVT_API_KEY,
        "private_key": Config.GRVT_PRIVATE_KEY,
        "subaccount_id": Config.GRVT_TRADING_ACCOUNT_ID,
        "env": Config.GRVT_ENV,
        "use_position_config": True,
    })

    try:
        await grvt.initialize()
    except Exception as e:
        report["initialize_error"] = str(e)
        md_lines.append(f"Initialize error: {e}")
        write_json(json_path, report)
        write_md(md_path, md_lines)
        return

    # Snapshot current initial leverage map
    snapshot_payload = {"sa": Config.GRVT_TRADING_ACCOUNT_ID}
    snapshot_before = None
    try:
        snapshot_before = await asyncio.to_thread(
            grvt.client._auth_and_post,
            leverage_snapshot_endpoint,
            payload=snapshot_payload,
        )
    except Exception as e:
        snapshot_before = {"error": str(e)}
    report["snapshot_before"] = {
        "endpoint": leverage_snapshot_endpoint,
        "payload": snapshot_payload,
        "response": to_plain(snapshot_before),
    }
    md_lines.extend(_render_block("snapshot_before", report["snapshot_before"]))

    for symbol in symbols:
        result = {
            "symbol": symbol,
            "set_position_config": {},
        }
        resp = None
        try:
            ok = await grvt.set_position_config(symbol, float(leverage), "CROSS")
            resp = {
                "ok": ok,
                "variant": getattr(grvt, "_last_position_config_variant", None),
                "payload": getattr(grvt, "_last_position_config_payload", None),
                "response": getattr(grvt, "_last_position_config_response", None),
            }
        except Exception as e:
            resp = {"error": str(e)}
        result["set_position_config"] = to_plain(resp)
        report["results"].append(result)
        md_lines.extend(_render_block(f"{symbol} set_position_config", result))

    # Snapshot after leverage set attempts
    snapshot_after = None
    try:
        snapshot_after = await asyncio.to_thread(
            grvt.client._auth_and_post,
            leverage_snapshot_endpoint,
            payload=snapshot_payload,
        )
    except Exception as e:
        snapshot_after = {"error": str(e)}
    report["snapshot_after"] = {
        "endpoint": leverage_snapshot_endpoint,
        "payload": snapshot_payload,
        "response": to_plain(snapshot_after),
    }
    md_lines.extend(_render_block("snapshot_after", report["snapshot_after"]))

    await grvt.close()
    write_json(json_path, report)
    write_md(md_path, md_lines)
    print(f"Wrote report: {json_path}")
    print(f"Wrote report: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
