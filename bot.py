# This example requires the 'members' and 'message_content' privileged intents to function.

import os
import asyncio
import traceback
import json
import requests

import discord
from discord.ext import commands, tasks
import dotenv

from celestenet import Celestenet

# --- ptvsd used for VSCode debugger ---
import ptvsd
ptvsd.enable_attach(address=('0.0.0.0', 5678))

# --- .env file contains bot token, cnet cookies, etc. ---
dotenv.load_dotenv()

class MyClient(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.celery: Celestenet = None

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')

    async def setup_hook(self) -> None:
        # start the task to run in the background
        self.task_loop_check.start()

    @tasks.loop(seconds=10)
    async def task_loop_check(self):
        try:
            if self.celery is None:
                self.celery = Celestenet()
                await self.celery.init_client(self, os.getenv("CNET_COOKIE"))
                await self.celery.load_phrases(os.getenv("PHRASE_FILTER_FILE"))
                print ("Celestenet wrapper instance init done.")
            self.celery.update_tasks()
        except Exception as catch_all:
            print ("socket_relay died")
            traceback.print_exception(catch_all)

    @task_loop_check.before_loop
    async def before_my_task(self):
        await self.wait_until_ready()  # wait until the bot logs in
        print ("Bot loop task has achieved ready")

intents=discord.Intents.default()
intents.members = True
intents.message_content = True
bot = MyClient(command_prefix='!', intents=intents, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False) )

@bot.command()
async def addlistener(ctx, chat_channel: discord.TextChannel, status_channel: discord.TextChannel, status_role: discord.Role):
    """Add a 'listener' to the Celestenet class instance."""
    if bot.celery is None:
        await ctx.send(f'Celestenet wrapper not ready ...')
        return
    await ctx.send(f'Setting up listener for {chat_channel.mention} / {status_channel.mention} / @ {status_role.name} ...')
    ret = await bot.celery.add_recipient(chat_channel, status_channel, status_role)
    await ctx.send(f'Setup returned with: {ret}')

@bot.command()
async def testping(ctx: commands.Context):
    if bot.celery is None:
        await ctx.send(f'Celestenet wrapper not ready ...')
        return
    bot.celery.ping_on_next_status = True
    await ctx.send(f'Bot will trigger ping on next status update.')

@bot.command()
#@bot.is_in_guild(os.getenv("BOT_GUILD"))
@commands.has_role(int(os.getenv("BOT_RESTARTER_ROLE")))
async def restart(ctx: commands.Context):
    payload=os.getenv("CNET_RESTART_REQ")
    try:
        payload=json.loads(payload)
    except json.JSONDecodeError:
        pass
    ret = requests.post(os.getenv("CNET_RESTART_URI"), data=payload)
    await ctx.send(f'Restart returned with: {ret.status_code} {ret.text}')

@bot.command()
@commands.has_role(int(os.getenv("BOT_RESTARTER_ROLE")))
async def reload(ctx: commands.Context):
    ret = await bot.celery.load_phrases(os.getenv("PHRASE_FILTER_FILE"))
    await ctx.send(f'Returned with: {ret}')

@bot.command()
@commands.has_role(int(os.getenv("BOT_RESTARTER_ROLE")))
async def check(ctx: commands.Context, phrase: str):
    found = False
    for p in bot.celery.phrases:
        m = p.search(phrase)
        if m is not None:
            await ctx.send(f"Match found: {p} => {m.group(0)}")
            found = True
    if not found:
        await ctx.send(f'No matches found for => {phrase}')

@bot.command()
@commands.has_role(int(os.getenv("BOT_RESTARTER_ROLE")))
async def prune_threads(ctx: commands.Context, days_old: int = 14):
    await bot.celery.prune_threads(days_old)

bot.run(os.getenv("BOT_TOKEN"))
