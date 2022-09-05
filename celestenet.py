from enum import Enum, auto
import traceback
import discord
from discord.ext import commands, tasks
import websockets
import os
import asyncio
import websockets
import json
import requests

class Player:
	def __init__(self, ID=0, name='', image=None):
		self.ID = ID
		self.name = name
		self.image = image

ws_commands = {}

class Celestenet:
	def __init__(self):
		"""Handles interactions with CN's JSON API and websocket
        """
		self.uri: str = 'wss://celestenet.0x0a.de/api/ws'
		self.origin: str = 'https://celestenet.0x0a.de'
		self.api_base: str = self.origin + '/api'

		self.task: asyncio.Task = None
		self.players: dict[Player] = {}
		self.mq: MessageQueue = MessageQueue()
		self.command_state: Celestenet.State = Celestenet.State.WaitForType
		self.command_current = None

		""" these three get set in init_client(...) below """
		self.client: discord.Client = None
		self.channel: discord.abc.GuildChannel = None
		self.cookies: dict | str = {}

	def init_client(self, client: discord.Client, cookies: dict | str, channel: discord.abc.GuildChannel):
		"""Initialize the discord.py client & channel refs and pass cookies

		Parameters
		----------
		client: discord.Client
			discord.py client or bot instance that can be used to send things as the bot.
		cookies: dict | str
			A dict of cookies or string that will be treated as json, that can then be
			passed to Celestenet requests for authentication.
		channel: discord.abc.GuildChannel
			discord.py channel reference to send messages to
        """
		self.client = client
		self.channel = channel
		if isinstance(cookies, str):
			self.cookies = json.loads(cookies)
		elif isinstance(cookies, dict):
			self.cookies = cookies

	def create_task(self):
		"""(re)creates the "socket_relay" main task and tracks it
        """
		if isinstance(self.task, asyncio.Task):
			if self.task.cancelled():
				self.task = None
			else:
				self.task.cancel()
		if self.task == None:
			self.task = self.client.loop.create_task(self._log_exception(self.socket_relay()))
		return self.task

	async def _log_exception(self, awaitable):
		"""Just a helper to catch exceptions from the asyncio task
        """
		try:
			return await awaitable
		except Exception as e:
			print (f"socket_relay died")
			traceback.print_exception(e)

	def get_task(self):
		"""Get the asyncio task but also performs checks if it's still "alive"
        """
		if isinstance(self.task, asyncio.Task):
			if self.task.cancelled():
				self.task = None
			if self.task.done():
				self.task = None
		return self.task

	def command_handler(cmd_fn):
		ws_commands[cmd_fn.__name__] = cmd_fn

	@command_handler
	async def chat(self, data: str):
		try:
			message = json.loads(data)
		except json.JSONDecodeError as e:
			error = f"Failed to parse chat payload: {data}"
			print(error)
			await self.channel.send(error)
			return
		
		print(message)
		if isinstance(message, dict):
			pid = message['PlayerID']
			chat_id = message['ID']
			author = self.players[pid].name if pid in self.players else pid
			icon = self.origin + self.players[pid].image if pid in self.players else None

			em = discord.Embed(description=message['Text'])
			em.set_author(name=str(author), icon_url = icon)
			# em.add_field(name="Chat:", value=message['Text'], inline=True)

			msg = self.mq.get_by_chat_id(chat_id)

			if msg:
				msg = await msg.edit(embed=em)
			else:
				msg = await self.channel.send(embed=em)
			
			self.mq.insert(chat_id, msg)

	@command_handler
	async def update(self, data: str):
		data = json.loads(data)
		print(f"cmd update: {data}")
		match data:
			case str(s) if s.endswith("/players"):
				self.get_players()
			case str(s) if s.endswith("/status"):
				self.get_status()
			case str(s) if s.endswith("/channels"):
				self.get_channels()
			case _:
				print(f"cmd update: {data} not implemented.")

	def api_fetch(self, endpoint: str, requests_method=requests.get, requests_data: str = None, raw: bool = False):
		"""Perform HTTP requests to Celestenet api
		
		Parameters
		----------
		endpoint: str
			Something like '/auth' that gets tacked onto the api base URI
		requests_method
			Just a lazy way so that this function can "wrap" requests.get, requests.post and whatever else
		requests_data: str, optional
			Content to send with the request, e.g. for POST
		raw: bool
			Function tries to parse successful responses as JSON unless this is set to True
        """
		response = requests_method(self.api_base + endpoint, data=requests_data, cookies=self.cookies)
		if response.status_code != 200:
			print(f"Failed api call {requests_method} to {endpoint} with status {response.status_code}")
			return None
		
		if raw:
			return response.text
		
		try:
			return response.json()
		except JSONDecodeError:
			print(f"Error decoding {endpoint} response: {response.text}")
			return None

	def get_players(self):
		"""Wrapper logic around /api/players
        """
		players = self.api_fetch("/players")
		if isinstance(players, list):
			for pdict in players:
				pid = pdict['ID']
				p = Player(
					ID=pdict['ID'], 
					name=pdict['FullName'],
					image=pdict['Avatar']
				)
				self.players[pid] = p

	def get_status(self):
		"""Wrapper logic around /api/status
        """
		status = self.api_fetch("/status")
		print(status)

	def get_channels(self):
		"""Wrapper logic around /api/channels
        """
		channels = self.api_fetch("/channels")
		print(channels)

	async def socket_relay(self):
		"""Async task that
			- tries to auth with cookies and such
			- opens and re-opens websocket connection with an endless loop
			- attempts auth against WS too
			- handles the various WS commands
			- sends discord messages like relaying the chat log
        """
		await self.client.wait_until_ready()
		print("Client ready.")
		
		authkey: str = None
		auth = self.api_fetch("/auth", requests.post, '""')

		if isinstance(auth, dict) and 'Key' in auth:
			authkey = auth['Key']
		else:
			print(f"Key not in reauth: {auth}")
		print(f"Auth'd to {authkey}")

		self.get_players()
		
		async for ws in websockets.connect(self.uri, origin=self.origin):
			if authkey:
				await ws.send("cmd")
				await ws.send("reauth")
				await ws.send(json.dumps(authkey))
			
			while True:
				message = ""
				try:
					while True:
						ws_data = await ws.recv()

						match self.command_state:
							case Celestenet.State.WaitForType:
								match ws_data:
									case "cmd":
										self.command_state = Celestenet.State.WaitForCMDID
									case "data":
										self.command_state = Celestenet.State.WaitForData
									case _:
										print(f"Unknown ws 'type': {ws_data}")
										break
						
							case Celestenet.State.WaitForCMDID:
								ws_cmd = None
								if ws_data in ws_commands:
									ws_cmd = ws_commands[ws_data]
								
								if ws_cmd is None:
									print(f"Unknown ws command: {ws_data}")
									break
								else:
									self.command_current = ws_cmd
									self.command_state = Celestenet.State.WaitForCMDPayload
							
							case Celestenet.State.WaitForCMDPayload:
								if self.command_current:
									await self.command_current(self, ws_data)
									self.command_state = Celestenet.State.WaitForType
									self.command_current = None
								else:
									print(f"Got payload but no current command: {ws_data}")
									self.command_current = None
									break

							case Celestenet.State.WaitForData:
								print(f"Got ws 'data' which isn't properly implemented: {ws_data}")
								self.command_state = Celestenet.State.WaitForType
								self.command_current = None

							case _:
								print(f"Unknown ws state: {self.command_state}")
								self.command_state = Celestenet.State.WaitForType
								self.command_current = None

				except websockets.ConnectionClosed:
					print("websocket died.")
					break
				self.mq.prune()
			print("We died.")

	class State(Enum):
		Invalid = -1

		WaitForType = auto()

		WaitForCMDID = auto()
		WaitForCMDPayload = auto()

		WaitForData = auto()
		

class MessageQueue:

	def __init__(self, mLen: int = 20):
		self.maxLen = mLen
		self.msg_ids: list[int] = []
		self.chat_to_msg_ids: dict[int] = {}
		self.msg_to_chat_ids: dict[int] = {}
		self.msg_refs: dict[discord.Message] = {}

	def prune(self):
		while len(self.msg_ids) > self.maxLen:
			drop_ref = self.msg_ids.pop()
			drop_msg = self.msg_refs.pop(drop_ref, None)
			self.chat_to_msg_ids.pop(drop_msg.chat_id, None)
	
	def get_by_chat_id(self, chat_id: int):
		if chat_id in self.chat_to_msg_ids:
			msg_id = self.chat_to_msg_ids[chat_id]
			return self.get_by_msg_id(msg_id)
		return None

	def get_by_msg_id(self, msg_id: int):
		if msg_id in self.msg_refs:
			return self.msg_refs[msg_id]
		return None

	def insert(self, chat_id: int, msg: discord.Message):
		self.msg_to_chat_ids[msg.id] = chat_id
		self.chat_to_msg_ids[chat_id] = msg.id
		self.msg_refs[msg.id] = msg
		self.msg_ids.insert(0, msg.id)