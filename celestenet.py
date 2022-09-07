from ctypes import ArgumentError
import datetime
from enum import Enum, auto
import re
import time
import traceback
from types import coroutine
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
        self.channel = None

    def dict_update(self, pd: dict):
        self.ID = pd.get('ID', self.ID) 
        self.name = pd.get('FullName', self.name)
        self.image = pd.get('Avatar', self.image)

class Channel:
    def __init__(self, ID=0, name='', players = [], private=False):
        self.ID = ID
        self.name = name
        self.private = private
        self.players = players

    def dict_update(self, pd: dict):
        self.ID = pd.get('ID', self.ID) 
        self.name = pd.get('Name', self.name)
        self.private = pd.get('IsPrivate', self.private)
        self.players = pd.get('Players', self.players)

class ChatRegexGroup:
    def __init__(self, d: dict):
        self.time = d.get('time', None)
        self.channel = d.get('channel', None)
        self.target = d.get('target', None)
        self.text = d.get('text', None)

ws_commands = {}

class Celestenet:
    def __init__(self):
        """Handles interactions with CN's JSON API and websocket
        """
        self.uri: str = 'wss://celestenet.0x0a.de/api/ws'
        self.origin: str = 'https://celestenet.0x0a.de'
        self.api_base: str = self.origin + '/api'

        self.tasks: dict[str, asyncio.Task] = {
            "socket": None,
            "process": None
        }
        self.task_routines: dict[str, coroutine] = {
            "socket": self.socket_relay,
            "process": self.process
        }
        self.players: dict[int, Player] = {}
        self.channels: dict[int, Channel] = {}
        self.mq: MessageQueue = MessageQueue()
        self.command_state: Celestenet.State = Celestenet.State.WaitForType
        self.command_current = None

        self.chat_regex = re.compile(r'(?sm)^\[(?P<time>[0-9:]+)\] (?: ?\[channel (?P<channel>.+)\])? ?(?::celestenet_avatar_[0-9]+_: )?[^ ]+(?: @ (?::celestenet_avatar_[0-9]+_: )?(?P<target>[^:]+))?: ?(?P<text>.*)$')

        """ these three get set in init_client(...) below """
        self.client: discord.Client = None
        self.channel: discord.abc.GuildChannel = None
        self.threads: dict[int, discord.Thread] = {}
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

    async def _log_exception(self, awaitable):
        """Just a helper to catch exceptions from the asyncio task
        """
        try:
            return await awaitable
        except Exception as e:
            print (f"socket_relay died")
            traceback.print_exception(e)

    def get_task(self, name):
        """Get the asyncio task
        """
        return self.tasks[name]

    def check_tasks(self):
        """checks if asyncio tasks are still "alive"
        """
        for t in self.tasks.keys():
            if self.tasks[t] is None:
                print (f"Creating task '{t}'")
                self.tasks[t] = self.client.loop.create_task(self._log_exception(self.task_routines[t]()))
            elif isinstance(self.tasks[t], asyncio.Task):
                if self.tasks[t].cancelled():
                    self.tasks[t] = None
                if self.tasks[t].done():
                    self.tasks[t] = None

    def chat_destructure(self, msg: str):
        match = self.chat_regex.match(msg)
        if match is None:
            print(f"Warning: Could not destructure chat message with Regex: {msg}")
            return None
        
        return ChatRegexGroup(match.groupdict())

    def get_channel_by_name(self, name):
            return next(filter(lambda c: c.name == name, self.channels.values()), None)

    def get_channel_by_id(self, ID):
            return self.channels.get(ID, None)

    async def get_or_make_thread(self, name):
        c: Channel = self.get_channel_by_name(name)
        if c:
            if c.ID not in self.threads:
                msg = await self.channel.send(f"Creating thread for channel: {name}")
                self.threads[c.ID] = await self.channel.create_thread(name = name, message = msg)
            return self.threads[c.ID]
        else:
            return None

    async def prune_threads(self):
        await self.channel.send("TODO: implement this :)))")

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
            pid: int = message['PlayerID']
            chat_id: int = message['ID']
            author: Player = self.players.get(pid, None)
            icon = (self.origin + author.image) if author and author.image else None

            content: ChatRegexGroup = self.chat_destructure(message['Text'])

            if content is None:
                await self.channel.send(f"Failed to parse chat message: {message}")
                return

            em = discord.Embed(description=content.text, timestamp = datetime.datetime.combine(datetime.date.today() , datetime.time.fromisoformat(content.time)))
            em.set_author(name=str(author.name) if author else pid, icon_url = icon)
            # em.add_field(name="Chat:", value=message['Text'], inline=True)

            dchan = self.channel
            print(f"Checking channel {content.channel} for {author} in {author.channel if author else ''}: {self.get_channel_by_id(author.channel) if author else ''}.")
            if author and content.channel:
                if self.get_channel_by_id(author.channel):
                    print(f"Creating/getting thread for channel {content.channel}...")
                    dchan = await self.get_or_make_thread(content.channel)
                    if dchan is None:
                        print(f"Failed to create/get thread for channel {content.channel}.")
                        dchan = self.channel
                else:
                    await self.channel.send(f"{author.name} sent message in {content.channel} but I have no knowledge of this channel. Auth issue?")

            await self.mq.insert_or_update(chat_id, pid, em = em, channel = dchan, purge=False if not author else content.target == author.name)

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
                if pid not in self.players:
                    p = Player()
                    self.players[pid] = p
                self.players[pid].dict_update(pdict)
    
    def get_status(self):
        """Wrapper logic around /api/status
        """
        status = self.api_fetch("/status")
        print(status)

    def get_channels(self):
        """Wrapper logic around /api/channels
        """
        channels = self.api_fetch("/channels")
        if isinstance(channels, list):
            for cdict in channels:
                cid = cdict['ID']
                if cid not in self.channels:
                    c = Channel()
                    self.channels[cid] = c
                self.channels[cid].dict_update(cdict)
                for pid in self.channels[cid].players:
                    if pid in self.players:
                        self.players[pid].channel = cid

    async def process(self):
        while True:
            # print("Going to prune MQ...")
            await self.mq.prune()
            # print("Going to process MQ...")
            await self.mq.process()
            print("Done processing.")
            await asyncio.sleep(1)

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
        self.get_channels()
        
        async for ws in websockets.connect(self.uri, origin=self.origin):
            if authkey:
                await ws.send("cmd")
                await ws.send("reauth")
                await ws.send(json.dumps(authkey))
            
            while True:
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
                    # self.prune_threads()
                except websockets.ConnectionClosed:
                    print("websocket died.")
                    break
            print("We died.")

    class State(Enum):
        Invalid = -1

        WaitForType = auto()

        WaitForCMDID = auto()
        WaitForCMDPayload = auto()

        WaitForData = auto()
        

class MessageQueue:

    class ChatMsg:
        def __init__(self, chat_id: int, user: int, channel: int = None):
            self.chat_id = chat_id
            self.user = user
            self.channel = channel

    class DiscordMsg:
        def __init__(self, em: discord.Embed, msg: discord.Message = None, channel: discord.TextChannel = None):
            self.embed = em
            self.msg = msg
            self.channel = channel
            self.sent = msg != None
        
        async def send(self, update: bool = False):
            if not self.sent:
                self.msg = await self.channel.send(embed=self.embed)
                self.sent = self.msg != None
            elif update and self.msg is not None:
                self.msg = await self.msg.edit(embed=self.embed)
                self.sent = self.msg != None

    class Message:
        def __init__(self, chat, discord):
            self.recv_time = MessageQueue.timestamp()
            self.chat: MessageQueue.ChatMsg = chat
            self.discord: MessageQueue.DiscordMsg = discord

    def timestamp():
        return int( time.time_ns() / 1000 )

    def __init__(self, max_len: int = 40, delay_before_send: int = 500):
        self.max_len = max_len
        self.delay_before_send = delay_before_send
        self.queue: list[MessageQueue.Message] = []
        self.lock = asyncio.Lock()

    async def prune(self):
        async with self.lock:
            while len(self.queue) > self.max_len:
                drop_msg = self.queue.pop()
                if not drop_msg.discord.sent:
                    print("Warning: popped message before it was sent!")
    
    def get_by_chat_id(self, chat_id: int):
        return next(filter(lambda m: m.chat.chat_id == chat_id, self.queue), None)

    def get_by_msg_id(self, msg_id: int):
        return next(filter(lambda m: isinstance(m.discord.msg, discord.Message) and m.discord.msg.id == msg_id, self.queue), None)

    async def insert_or_update(self, chat_id: int, user_id: int, em: discord.Embed, msg: discord.Message = None, channel: discord.TextChannel = None, purge: bool = False):
        async with self.lock:
            found_msg = self.get_by_chat_id(chat_id)

            print(f"Checking for {chat_id}: {found_msg}")

            if isinstance(found_msg, MessageQueue.Message):
                if purge:
                    print(f"Purging from MQ: {found_msg.chat.chat_id} {user_id}")
                    self.queue.remove(found_msg)
                    return
                found_msg.chat.user = user_id
                found_msg.discord.embed = em
                found_msg.discord.msg = msg
                found_msg.discord.channel = channel
                return
            
            new_msg = MessageQueue.Message(
                MessageQueue.ChatMsg(chat_id, user_id),
                MessageQueue.DiscordMsg(em, msg, channel)
            )

            print(f"[{MessageQueue.timestamp()}] Inserting into MQ: {new_msg.recv_time} {new_msg.chat.chat_id} {new_msg.discord.channel}")
            self.queue.insert(0, new_msg)

    async def process(self):
        async with self.lock:
            for m in reversed(self.queue):
                if not m.discord.sent and MessageQueue.timestamp() - m.recv_time > self.delay_before_send:
                    await m.discord.send()