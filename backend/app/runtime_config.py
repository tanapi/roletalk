from __future__ import annotations

import json
import os


RUNTIME_CONFIG_HEADER = "x-roletalk-runtime-config"


def apply_runtime_config_header(header_value: str | None) -> None:
    if not header_value:
        return
    try:
        config = json.loads(header_value)
    except json.JSONDecodeError:
        return
    if not isinstance(config, dict):
        return
    for key, value in config.items():
        if not isinstance(key, str):
            continue
        if value is None:
            os.environ.pop(key, None)
            continue
        os.environ[key] = str(value)
