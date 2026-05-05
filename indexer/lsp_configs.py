"""Per-language LSP server configurations for Toroidal-Indexer Tier 2."""

import os
import shutil
from dataclasses import dataclass, field


@dataclass
class LSPServerConfig:
    command: str
    args: list[str]
    language_id: str
    extensions: list[str]
    init_options: dict = field(default_factory=dict)


CONFIGS: dict[str, LSPServerConfig] = {
    "python": LSPServerConfig(
        command="pyright-langserver",
        args=["--stdio"],
        language_id="python",
        extensions=[".py"],
        init_options={},
    ),
    "typescript": LSPServerConfig(
        command="typescript-language-server",
        args=["--stdio"],
        language_id="typescript",
        extensions=[".ts", ".tsx", ".js", ".jsx"],
        init_options={},
    ),
    "rust": LSPServerConfig(
        command="rustup",
        args=["run", "nightly", "rust-analyzer"],
        language_id="rust",
        extensions=[".rs"],
        init_options={"cargo": {"buildScripts": {"enable": True}}},
    ),
    "go": LSPServerConfig(
        command="gopls",
        args=["serve"],
        language_id="go",
        extensions=[".go"],
        init_options={},
    ),
}

_EXT_TO_LANG: dict[str, str] = {}
for _lang, _cfg in CONFIGS.items():
    for _ext in _cfg.extensions:
        _EXT_TO_LANG[_ext] = _lang


def get_config_for_file(file_path: str) -> LSPServerConfig | None:
    ext = os.path.splitext(file_path)[1]
    lang = _EXT_TO_LANG.get(ext)
    if lang is None:
        return None
    return CONFIGS[lang]


def is_server_available(config: LSPServerConfig) -> bool:
    return shutil.which(config.command) is not None
