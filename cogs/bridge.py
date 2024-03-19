import asyncio
import json
import os
import re
from typing import cast
from uuid import uuid4

import discord
import websockets
from discord.backoff import ExponentialBackoff
from discord.ext import commands
from discord.ext import tasks

from common import Message

EMOJI = re.compile(r"<a?(:[^:]+:)\d+>")
USER_MENTION = re.compile(r"<@!?(\d+)>")
CHANNEL_MENTION = re.compile(r"<#?(\d+)>")


def strip_non_ascii(string):
    return "".join(c for c in string if 0 < ord(c) < 127)


class Bridge(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ws: websockets.WebSocketClientProtocol = ...
        self.channel = bot.get_channel(int(os.environ["BRIDGE_CHANNEL"]))
        self.sent: set[str] = set()
        self.backoff = ExponentialBackoff()
        self.backoff._max = 5

    async def cog_unload(self) -> None:
        self.ws_handler.cancel()
        await self.ws.close()

    async def init_ws(self):
        self.ws = await websockets.connect(
            f"ws://localhost:{os.environ['BRIDGE_PORT']}/bot/{os.environ['BOT_KEY']}"
        )

    def sub_mentions(self, message: str) -> str:
        for mention in USER_MENTION.finditer(message):
            user_id = int(mention.group(1))
            user = self.channel.guild.get_member(user_id)
            if user:
                message = message.replace(mention.group(0), f"@{user.display_name}")

        for mention in CHANNEL_MENTION.finditer(message):
            channel_id = int(mention.group(1))
            channel = self.channel.guild.get_channel(channel_id)
            if channel:
                message = message.replace(mention.group(0), f"#{channel}")

        return message

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (
            message.author.bot
            or message.content.startswith(self.bot.user.mention)
            or message.channel.id != self.channel.id
            or not message.content
        ):
            return

        content = message.content.replace("\n", " ")
        content = EMOJI.sub(r"\1", content)
        content = self.sub_mentions(content)
        # 1.8.9 is 10 fucking years old and has no concept of any non-ASCII characters in its
        # default font rendering, so just enforce ASCII to dodge the rendering issues entirely
        content = strip_non_ascii(content)

        if not content:
            return
        elif len(content) > 256:
            await message.reply(
                "Message was truncated to be under 256 characters long",
                allowed_mentions=discord.AllowedMentions.none(),
                delete_after=5,
            )
            content = content[:256]

        nonce = uuid4()
        self.sent.add(str(nonce))
        await self.ws.send(
            json.dumps(
                {
                    "author": (
                        strip_non_ascii(message.author.display_name)
                        or str(message.author)
                    ),
                    "message": content,
                    "nonce": str(nonce),
                }
            )
        )

    @tasks.loop()
    async def ws_handler(self):
        try:
            async for message in self.ws:
                data: Message = cast(Message, json.loads(message))

                if data["nonce"] in self.sent:
                    self.sent.discard(data["nonce"])
                    continue

                await self.channel.send(
                    f"**{data['author']}**: {data['message']}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except websockets.ConnectionClosedError:
            delay = self.backoff.delay()
            print(f"Websocket connection closed, waiting {delay} to reconnect")
            await asyncio.sleep(delay)
            await self.init_ws()


async def setup(bot: commands.Bot):
    cog = Bridge(bot)
    await cog.init_ws()
    cog.ws_handler.start()
    await bot.add_cog(cog)
