import asyncio
import html
import os

import telegram
from dotenv import load_dotenv
from telegram import Bot, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.request import HTTPXRequest

load_dotenv()


def get_creds():
    return os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")


def describe(exc):
    skip = {"object", "BaseException", "Exception"}
    chain = [c.__name__ for c in type(exc).__mro__ if c.__name__ not in skip]
    print("   FAILED:", " > ".join(chain))
    print("   message:", exc)


async def probe(label, call):
    print(f"\n[{label}]")
    try:
        await call()
        print("   OK")
    except TelegramError as exc:
        describe(exc)
    except Exception as exc:  # anything that is not a Telegram error
        print("   NON-TELEGRAM ERROR:", type(exc).__name__, exc)


async def main():
    token, chat_id = get_creds()
    print("python-telegram-bot version:", telegram.__version__)
    print("token set:", bool(token), "| chat id set:", bool(chat_id))
    if not token or not chat_id:
        print("Fix .env first.")
        return

    name = "Solo Leveling: Ragnarok <Side & Story>"
    url = "https://example.com/series/solo?a=1&b=2"
    html_text = (
        f'<a href="{html.escape(url, quote=True)}">{html.escape(name)}</a>'
        " - Ch. 631"
    )

    async with Bot(token) as bot:

        async def get_me():
            me = await bot.get_me()
            print("   bot username:", me.username)

        async def plain():
            await bot.send_message(chat_id, "ScrollPing test: plain message")

        async def formatted():
            await bot.send_message(
                chat_id,
                html_text,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

        async def bad_chat():
            await bot.send_message(1, "x")

        async def bad_html():
            await bot.send_message(chat_id, "<b>unclosed", parse_mode=ParseMode.HTML)

        async def empty_text():
            await bot.send_message(chat_id, "")

        async def too_long():
            await bot.send_message(chat_id, "a" * 5000)

        print("\n=== REAL SENDS (2 messages should arrive) ===")
        await probe("get_me", get_me)
        await probe("plain send", plain)
        await probe("HTML send with link", formatted)

        print("\n=== DELIBERATE FAILURES (nothing should arrive) ===")
        await probe("unknown chat id", bad_chat)
        await probe("malformed HTML", bad_html)
        await probe("empty text", empty_text)
        await probe("message over 4096 chars", too_long)

    async def invalid_token():
        async with Bot("123456:not-a-real-token") as b:
            await b.send_message(chat_id, "x")

    async def unreachable():
        async with Bot(token, base_url="http://localhost:1/bot") as b:
            await b.send_message(chat_id, "x")

    async def tiny_timeout():
        async with Bot(token, request=HTTPXRequest(connect_timeout=0.001)) as b:
            await b.send_message(chat_id, "x")

    await probe("invalid token", invalid_token)
    await probe("unreachable server", unreachable)
    await probe("tiny connect timeout", tiny_timeout)


if __name__ == "__main__":
    asyncio.run(main())