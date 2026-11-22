"""Compatibility entry point for hosts configured to run `python main.py`."""

from __future__ import annotations

import asyncio
import logging

from bot import main


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger("playfunpay").info("Bot stopped")
