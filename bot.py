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
celery = Celestenet()
celery_channel = None

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')
    try:
        global celery_channel
        celery_channel = await channel_setup(os.getenv("CHANNEL"))
        celery.init_client(bot, None, celery_channel) # os.getenv("CNET_COOKIE")
        celery.check_tasks()
    except Exception as catch_all:
        print ("socket_relay died")
        traceback.print_exception(catch_all)

async def channel_setup(name):
    channel_found = None
    for channel in bot.get_all_channels():
        if channel.name == name:
            channel_found = channel
            break
    if channel_found is not None:
        await channel_found.send("Celestenet bot configured to use this channel.")
    else:
        print(f"Channel {name} not found!")
    return channel_found

@tasks.loop(seconds=3)
async def task_loop_check():
    print("Checking tasks...")
    celery.check_tasks()

async def main():
    async with bot:
        await bot.start(os.getenv("BOT_TOKEN"))


asyncio.run(main())