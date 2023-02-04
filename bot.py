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
        self.user_color: dict[int, str] = {}
        self.dc_emote_replace: dict[str, str] = {}
        self.cnet_emote_replace: dict[str, str] = {}

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
                await self.celery.load_recipients(os.getenv("CHANNELS_ROLE_FILE"))

                self.user_color = await self.load_env_json("USER_COLOR_FILE")
                self.dc_emote_replace = await self.load_env_json("DC_EMOTES_FILE")
                self.cnet_emote_replace = await self.load_env_json("CNET_EMOTES_FILE")
                await self.celery.set_emote_replaces(self.cnet_emote_replace)

                print ("Celestenet wrapper instance init done.")
            self.celery.update_tasks()
        except Exception as catch_all:
            print ("socket_relay died")
            traceback.print_exception(catch_all)

    async def load_env_json(self, env_json_file: str):
        try:
            with open(os.getenv(env_json_file)) as f:
                output = json.load(f)
        except Exception as catch_all:
            print(f'Failed to load json from {env_json_file}={os.getenv(env_json_file)}')
            traceback.print_exception(catch_all)
        return output

    async def write_env_json(self, env_json_file: str, output):
        try:
            with open(os.getenv(env_json_file), "w") as f:
                 json.dump(output, f)
        except Exception as catch_all:
            print(f'Failed to write json to {env_json_file}={os.getenv(env_json_file)}')
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
@commands.has_role(int(os.getenv("BOT_RESTARTER_ROLE")))
async def addlistener(ctx, chat_channel: discord.TextChannel, status_channel: discord.TextChannel, status_role: discord.Role):
    """Add a 'listener' to the Celestenet class instance."""
    if bot.celery is None:
        await ctx.send(f'Celestenet wrapper not ready ...')
        return
    await ctx.send(f'Setting up listener for {chat_channel.mention} / {status_channel.mention} / @ {status_role.name} ...')
    ret = await bot.celery.add_recipient(chat_channel, status_channel, status_role)
    await ctx.send(f'Setup returned with: {ret}')

@bot.command()
@commands.has_role(int(os.getenv("BOT_RESTARTER_ROLE")))
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

@bot.command()
@commands.has_role(int(os.getenv("BOT_RESTARTER_ROLE")))
async def set_color(ctx: commands.Context, col: str):
    if not col.startswith("#"):
        await ctx.send(f"Color '{col}' doesn't start with #")
    try:
        int(col[1:], 16)
    except ValueError:
        await ctx.send(f"Color '{col}' does not look hexadecimal.")
    bot.user_color[ctx.message.author.id] = col

    await bot.write_env_json("USER_COLOR_FILE", bot.user_color)

    await ctx.send(f"Set {ctx.message.author} color to '{bot.user_color[ctx.message.author.id]}'.")

@bot.command()
@commands.has_role(int(os.getenv("BOT_RESTARTER_ROLE")))
async def set_dc_emote(ctx: commands.Context, discord_emote: str, replacement: str):
    bot.dc_emote_replace[str(discord_emote)] = f":{replacement}:"

    await bot.write_env_json("DC_EMOTES_FILE", bot.dc_emote_replace)

    await ctx.send(f"Set '\\{discord_emote}' to '\\{bot.dc_emote_replace[str(discord_emote)]}' for outgoing.")

@bot.command()
@commands.has_role(int(os.getenv("BOT_RESTARTER_ROLE")))
async def set_cnet_emote(ctx: commands.Context, cnet_emote: str, replacement: str):
    cnet_emote = f":{cnet_emote}:"
    bot.cnet_emote_replace[cnet_emote] = replacement

    await bot.celery.set_emote_replaces(bot.cnet_emote_replace)
    await bot.write_env_json("CNET_EMOTES_FILE", bot.cnet_emote_replace)

    await ctx.send(f"Set '\\{cnet_emote}' to '{bot.cnet_emote_replace[str(cnet_emote)]}' for incoming.")

@bot.command()
@commands.has_role(int(os.getenv("BOT_RESTARTER_ROLE")))
async def get_color(ctx: commands.Context):
    color = bot.user_color.get(ctx.message.author.id, None)
    if color is None:
        color = bot.user_color.get(str(ctx.message.author.id), None)
    await ctx.send(f'User {ctx.message.author.id} ({type(ctx.message.author.id)}) has color: {color}')


@bot.command()
@commands.has_role(int(os.getenv("BOT_RESTARTER_ROLE")))
async def say(ctx: commands.Context, *, message: str):
    color = bot.user_color.get(ctx.message.author.id, None)
    if color is None:
        color = bot.user_color.get(str(ctx.message.author.id), None)
    old_msg = message
    for e, r in bot.dc_emote_replace.items():
        message = message.replace(e, r)
    print(f"{ctx.message.author} ran !say {message}")

    if old_msg != message:
        await ctx.send(f"Message '{discord.utils.escape_markdown(old_msg)}' became '{discord.utils.escape_markdown(message)}' due to emote replacements.")

    if color:
        ret = await bot.celery.invoke_wscmd("chatx", {"Color": color, "Text": message})
    else:
        ret = await bot.celery.invoke_wscmd("chat", message)
    await ctx.send(f'Returned with: {ret}')

bot.run(os.getenv("BOT_TOKEN"))
