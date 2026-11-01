"""設定の読み込み。環境依存の値は config.json の 1 か所にまとめる。

探す順: 引数 --config → 環境変数 DUI_CONFIG → この道具と同じ場所の config.json
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def config_path(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get("DUI_CONFIG") or HERE / "config.json").expanduser().resolve()


def resolve(base: Path, value: str) -> Path:
    p = Path(os.path.expanduser(value))
    return p if p.is_absolute() else (base / p).resolve()


def load(explicit: str | None = None) -> dict:
    path = config_path(explicit)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    cfg["_path"] = str(path)
    for key in ("python", "irodori_dir", "check_script", "output_dir"):
        if cfg.get(key):
            cfg[key] = str(resolve(base, cfg[key]))
    cfg["voice"]["ref_wav"] = str(resolve(base, cfg["voice"]["ref_wav"])) if cfg["voice"].get("ref_wav") else None
    for kind, items in cfg["fonts"].items():
        cfg["fonts"][kind] = [str(resolve(base, f)) for f in items]
    return cfg


def first_existing(paths: list[str]) -> str | None:
    return next((p for p in paths if Path(p).exists()), None)
