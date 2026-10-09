"""taotrader: research, backtest and paper trading of micro-cap Bittensor dTAO subnet alpha (docs/DESIGN.md).

The live adapter (taotrader.live) is Linux/WSL-only, gated, and imported only by the CLI behind the gate.
"""
from __future__ import annotations

__version__ = "0.1.0"


def main() -> int:
    """Entry point shim: delegates to taotrader.cli.main (WP12 owns the CLI)."""
    from .cli import main as cli_main

    return cli_main()
