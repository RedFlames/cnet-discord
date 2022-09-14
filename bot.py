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

description = '''
Bot description goes here lol.'''

intents=discord.Intents(guilds=True, members=True, message_content=True)
bot = commands.Bot(command_prefix='!', description=description, intents=intents)
celery: Celestenet = None

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')


@tasks.loop(seconds=10)
async def task_loop_check():
    await bot.wait_until_ready()
    #print("Checking tasks...")
    global celery
    if celery is None:
        try:
            celery = Celestenet()
            await celery.init_client(bot, os.getenv("CNET_COOKIE"), os.getenv("CHANNEL_PREFIX"))
            celery.check_tasks()
        except Exception as catch_all:
            print ("socket_relay died")
            traceback.print_exception(catch_all)
    else:
        celery.check_tasks()

async def main():
    async with bot:
        #bot.loop.create_task(task_loop_check())
        task_loop_check.start()
        await bot.start(os.getenv("BOT_TOKEN"))


asyncio.run(main())
