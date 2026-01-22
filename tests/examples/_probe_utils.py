import json
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv


def project_root() -> Path:
    # examples/ -> tests/ -> shared_crypto_lib/ -> src/ -> project
    return Path(__file__).resolve().parents[4]


def load_env() -> Path:
    env_path = project_root() / "src" / "GRVT_Lighter_Bot" / ".env"
    load_dotenv(env_path)
    return env_path


def output_dir() -> Path:
    out = project_root() / "src" / "shared_crypto_lib" / "tests" / "examples_outputs"
    out.mkdir(parents=True, exist_ok=True)
    return out


def now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def to_plain(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_plain(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {k: to_plain(v) for k, v in obj.__dict__.items()}
    return obj


def quantize(value: float, step: float) -> float:
    if step and step > 0:
        return round(value / step) * step
    return value


def sleep_until(deadline_ts: float, interval_s: float) -> float:
    now = time.time()
    if now >= deadline_ts:
        return 0.0
    return min(interval_s, deadline_ts - now)


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=str), encoding="utf-8")


def write_md(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
