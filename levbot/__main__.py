"""Starts the leverage bot and its panel: python -m levbot"""

import asyncio
import logging
import sys

import aiohttp
from aiohttp import web

from . import config
from .binance import Binance, Signer
from .engine import Engine, load_state, save_state
from .web import make_app


async def telegram_sender(session: aiohttp.ClientSession):
    if not (config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID):
        return None

    async def send(text: str):
        url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
        async with session.post(url, json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text}, timeout=aiohttp.ClientTimeout(total=10)):
            pass
    return send


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if config.MODE == "live" and not config.LIVE_ARMED:
        sys.exit("TRADE_MODE=live needs LIVE_ARMED=YES: refusing to start")
    if not config.API_KEY or not config.KEY_FILE:
        sys.exit("API key or key file missing")
    async with aiohttp.ClientSession() as session:
        client = Binance(session, signer=Signer(config.KEY_FILE))
        engine = Engine(client, load_state(), await telegram_sender(session))
        runner = web.AppRunner(make_app(engine))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", config.PORT).start()
        await engine.tell(f"🤖 Kaldıraç botu başladı ({'GERÇEK PARA' if config.MODE == 'live' else 'demo, sahte para'}). "
                          f"Bütçe {config.BUDGET_USD:g}$, işlem başı en çok {config.MARGIN_PER_TRADE_USD:g}$ teminat.")
        try:
            await engine.run(lambda: save_state(engine.state))
        finally:
            save_state(engine.state)


if __name__ == "__main__":
    asyncio.run(main())
