
import discord
from discord.ext import commands, tasks
import websockets



class Celestenet(commands.Cog):
	def __init__(self, client):
		self.client = client
		self.uri = 'wss://celestenet.0x0a.de/api/ws'
		self.ws = None
		self.popper.start()
		self.queue = []
		


	@tasks.loop(seconds=1)
	async def popper(self):
		channel = await self.client.fetch_channel(158222673850269696)
		while len(self.queue) > 0:
			message = self.queue.pop()
			print(message)
			print("making embed")
			embed = discord.Embed(title="New ws", color = 0xff0000, url='',)
			embed.add_field(name="Content:", value=message, inline=True)        
		
			await channel.send(embed=embed)

	@popper.before_loop
	async def before_popper(self):
		channel = await self.client.fetch_channel(158222673850269696)
		await self.client.wait_until_ready()  # wait until the bot logs in
		self.ws = await websockets.connect(self.uri)
		await channel.send("WS connected!")

#	@commands.Cog.listener()
#	async def on_ready(self):
#		channel = await self.client.fetch_channel(158222673850269696)
#		await channel.send("starting cog loop")
		
		


async def setup(client):
	await client.add_cog(Celestenet(client))