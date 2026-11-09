"""Entry point: the Binance leverage bot and its panel (python main.py)."""

import asyncio

from levbot.__main__ import main

if __name__ == "__main__":
    asyncio.run(main())
