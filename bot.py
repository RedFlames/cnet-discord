# This example requires the 'members' and 'message_content' privileged intents to function.


import os
import random
import asyncio
import json
import traceback

import discord
from discord.ext import commands, tasks
import dotenv
import requests
import websockets

from celestenet import Celestenet

# --- ptvsd used for VSCode debugger ---
import ptvsd
ptvsd.enable_attach(address=('0.0.0.0', 5678))

# --- .env file contains bot token, cnet cookies, etc. ---
dotenv.load_dotenv()

description = '''
Bot description goes here lol.'''

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', description=description, intents=intents)
celery = Celestenet()
celery_channel = None
celery_task = None

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')
    try:
        celery_channel = await channel_setup(os.getenv("CHANNEL"))
        celery.init_client(bot, os.getenv("CNET_COOKIE"), celery_channel)
        celery.create_task()
        print("Starting task loop...")
        task_loop_check.start()
    except Exception as e:
        print (f"socket_relay died")
        traceback.print_exception(e)

async def channel_setup(name):
    channel_found = None
    for channel in bot.get_all_channels():
        if channel.name == name:
            channel_found = channel
            break
    if channel_found != None:
        await channel_found.send(f"Celestenet bot configured to use this channel.")
    else:
        print(f"Channel {name} not found!")
    return channel_found

@tasks.loop(seconds=2)
async def task_loop_check():
    if celery.get_task() is ct == None:
        celery_channel.send("Celery task ended, restarting...")
        celery.create_task()
    print(ct)

@bot.command()
async def add(ctx, left: int, right: int):
    """Adds two numbers together."""
    await ctx.send(left + right)


@bot.command()
async def roll(ctx, dice: str):
    """Rolls a dice in NdN format."""
    try:
        rolls, limit = map(int, dice.split('d'))
    except Exception:
        await ctx.send('Format has to be in NdN!')
        return

    result = ', '.join(str(random.randint(1, limit)) for r in range(rolls))
    await ctx.send(result)


@bot.command(description='For when you wanna settle the score some other way')
async def choose(ctx, *choices: str):
    """Chooses between multiple choices."""
    await ctx.send(random.choice(choices))


@bot.command()
async def repeat(ctx, times: int, content='repeating...'):
    """Repeats a message multiple times."""
    for i in range(times):
        await ctx.send(content)


@bot.command()
async def joined(ctx, member: discord.Member):
    """Says when a member joined."""
    await ctx.send(f'{member.name} joined {discord.utils.format_dt(member.joined_at)}')


@bot.group()
async def cool(ctx):
    """Says if a user is cool.
    In reality this just checks if a subcommand is being invoked.
    """
    if ctx.invoked_subcommand is None:
        await ctx.send(f'No, {ctx.subcommand_passed} is not cool')


@cool.command(name='bot')
async def _bot(ctx):
    """Is the bot cool?"""
    await ctx.send('Yes, the bot is cool.')

async def main():
    async with bot:
        await bot.start(os.getenv("BOT_TOKEN"))


asyncio.run(main())