# This example requires the 'members' and 'message_content' privileged intents to function.

import os
import asyncio
import traceback

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
        #print("Checking tasks...")
        if self.celery is None:
            try:
                self.celery = Celestenet()
                await self.celery.init_client(self, os.getenv("CNET_COOKIE"))
                self.celery.check_tasks()
            except Exception as catch_all:
                print ("socket_relay died")
                traceback.print_exception(catch_all)
        else:
            self.celery.check_tasks()

    @task_loop_check.before_loop
    async def before_my_task(self):
        await self.wait_until_ready()  # wait until the bot logs in

intents=discord.Intents.default()
intents.members = True
intents.message_content = True
bot = MyClient(command_prefix='!', intents=intents)

@bot.command()
async def addlistener(ctx, chat_channel: discord.TextChannel, status_channel: discord.TextChannel, status_role: discord.Role):
    """Add a 'listener' to the Celestenet class instance."""
    if bot.celery is None:
        await ctx.send(f'Celestenet wrapper not ready ...')
        return
    await ctx.send(f'Setting up listener for {chat_channel.mention} / {status_channel.mention} / @ {status_role.name} ...')
    ret = await bot.celery.add_recipient(chat_channel, status_channel, status_role)
    await ctx.send(f'Setup returned with: {ret}')

bot.run(os.getenv("BOT_TOKEN"))