# This example requires the 'members' and 'message_content' privileged intents to function.

import discord
from discord.ext import commands
import random
import os
import dotenv
import asyncio
#import websocket
import websockets
import json
import requests
import traceback

dotenv.load_dotenv()

description = '''An example bot to showcase the discord.ext.commands extension
module.
There are a number of utility commands being showcased here.'''

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='?', description=description, intents=intents)

@bot.event
async def on_ready():
	print(f'Logged in as {bot.user} (ID: {bot.user.id})')
	print('------')
	try:
		await webhooker(bot) #bot.add_cog(Celestenet(bot))
	except Exception as e:
		print (f"websocketeer died")
		traceback.print_exception(e)


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

async def webhooker(client):
	uri = 'wss://celestenet.0x0a.de/api/ws'
	ori='https://celestenet.0x0a.de'
	print({"Cookie": os.getenv("CNET_COOKIE")})
	cookies = json.loads(os.getenv("CNET_COOKIE"))
	
	await client.wait_until_ready()
	channel = await client.fetch_channel(158222673850269696)
	print("Client ready.")
	
	rd = None
	authkey = None
	r = requests.post("https://celestenet.0x0a.de/api/auth", '""', cookies=cookies)
	if r.status_code != 200:
		print("Failed api/auth")
	else:
		try:
			rd = r.json()
		except JSONDecodeError:
			print(f"Error decoding reauth: {r.text}")
		
		if isinstance(rd, dict) and 'Key' in rd:
			authkey = rd['Key']
		else:
			print(f"Key not in reauth: {rd}")
		print(f"Auth'd to {authkey}")
	
	async for ws in websockets.connect(uri, origin=ori):
		if authkey:
			await asyncio.sleep(1)
			await ws.send("cmd")
			await ws.send("reauth")
			await ws.send(json.dumps(authkey))
		
		while True:
			message = ""
			try:
				while True:
					ws_cmd = await ws.recv()
					print(f"ws_cmd: {ws_cmd}")
					if ws_cmd == "chat":
						ws_msg = await ws.recv()
						print(f"ws_msg: {ws_msg}")
						try:
							message = json.loads(ws_msg)
							break
						except JSONDecodeError:
							print(f"Error decoding chat: {ws_msg}")
			except websockets.ConnectionClosed:
				print("websocket died.")
				break
			print(message)
			if isinstance(message, dict):
				em = discord.Embed(title=message['PlayerID'])
				em.add_field(name="Chat:", value=message['Text'], inline=True)
				print(em)
				await channel.send(embed=em)
		print("We died.")
		


async def main():
	async with bot:
		#input_coroutines = [bot.start(os.getenv("BOT_TOKEN"))]
		#await bot.load_extension('celestenet')
		#return await asyncio.gather(*input_coroutines, return_exceptions=True)
		await bot.start(os.getenv("BOT_TOKEN"))


asyncio.run(main())