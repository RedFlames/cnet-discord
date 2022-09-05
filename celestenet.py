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

class Celestenet:
	def __init__(self):
		self.client: discord.Client = None
		self.channel = None
		self.cookies: dict | str = {}
		self.uri: str = 'wss://celestenet.0x0a.de/api/ws'
		self.origin: str = 'https://celestenet.0x0a.de'
		self.api_base: str = self.origin + '/api'
		self.task: asyncio.Task = None
		self.players: dict[Player] = {}
		self.mq: MessageQueue = MessageQueue()

	def init_client(self, client: discord.Client, cookies: dict | str, channel):
		self.client = client
		self.channel = channel
		if isinstance(cookies, str):
			self.cookies = json.loads(cookies)
		elif isinstance(cookies, dict):
			self.cookies = cookies

	def create_task(self):
		if isinstance(self.task, asyncio.Task):
			if self.task.cancelled():
				self.task = None
			else:
				self.task.cancel()
		if self.task == None:
			self.task = self.client.loop.create_task(self._log_exception(self.socket_relay()))
		return self.task

	async def _log_exception(self, awaitable):
		try:
			return await awaitable
		except Exception as e:
			print (f"socket_relay died")
			traceback.print_exception(e)

	def get_task(self):
		if isinstance(self.task, asyncio.Task):
			if self.task.cancelled():
				self.task = None
			if self.task.done():
				self.task = None
		return self.task

	def api_fetch(self, endpoint: str, requests_method=requests.get, requests_data: str = None, raw: bool = False):
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

	async def socket_relay(self):
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
						ws_cmd = await ws.recv()
						print(f"ws_cmd: {ws_cmd}")
						# TODO: handle all "commands"
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
				self.mq.prune()
			print("We died.")


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