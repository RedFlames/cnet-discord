import datetime
from zoneinfo import ZoneInfo
from enum import Enum, auto
import re
import time
import traceback
from types import coroutine
from typing import Any
import discord
import websockets
import asyncio
import json
import requests
from textwrap import dedent

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
    def __init__(self, ID=0, name='', players = None, private=False):
        self.ID = ID
        self.name = name
        self.private = private
        self.players = [] if players is None else players

    def dict_update(self, pd: dict):
        self.ID = pd.get('ID', self.ID)
        self.name = pd.get('Name', self.name)
        self.private = pd.get('IsPrivate', self.private)
        self.players = pd.get('Players', self.players)

class ChatRegexGroup:
    def __init__(self, d: dict):
        self.time: str | None = d.get('time', None)
        self.channel: str | None = d.get('channel', None)
        self.whisper: str | None = d.get('whisper', None)
        self.target: str | None = d.get('target', None)
        self.text: str | None = d.get('text', None)

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
        self.status: dict[str, Any] = {}
        self.activity: discord.Activity = discord.Activity(
                        name="Celestenet",
                        type=discord.ActivityType.watching,
                        details="Starting...", timestamps={"start": datetime.datetime.now().timestamp})

        self.chat_regex = re.compile(r'(?sm)^\[(?P<time>[0-9:]+)\] (?: ?\[(?P<whisper>whisper)[^]]*\])? ?(?: ?\[channel (?P<channel>.+)\])? ?(?::celestenet_avatar_[0-9]+_: )?[^ ]+(?: @ (?::celestenet_avatar_[0-9]+_: )?(?P<target>[^:]+))?: ?(?P<text>.*)$')

        """ these three get set in init_client(...) below """
        self.client: discord.Client = None
        self.channel: discord.abc.GuildChannel = None
        self.status_channel: discord.abc.GuildChannel = None
        self.status_role = None
        self.last_status_msg: discord.Message = None
        self.last_status_timestamp = datetime.datetime.now() - datetime.timedelta(minutes=5)
        self.threads: dict[int, discord.Thread] = {}
        self.cookies: dict = {}

        self.server_chat_id = (1 << 32) - 1
        self.players[self.server_chat_id] = Player(self.server_chat_id, "** SERVER **")

    async def init_client(self, client: discord.Client, cookies: dict | str, channel_prefix: str):
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
        self.channel = await self.channel_setup(channel_prefix + "chat")
        self.status_channel = await self.channel_setup(channel_prefix + "status")
        self.status_role = next(filter(lambda r: r.name == channel_prefix + "status", self.status_channel.guild.roles), None)
        if isinstance(cookies, str):
            self.cookies = json.loads(cookies)
        elif isinstance(cookies, dict):
            self.cookies = cookies

    async def channel_setup(self, name):
        channel_found = None
        for channel in self.client.get_all_channels():
            if channel.name == name:
                channel_found = channel
                break
        if channel_found is not None:
            await channel_found.send("Celestenet bot configured to use this channel.")
        else:
            print(f"Channel {name} not found!")
        return channel_found

    async def _log_exception(self, name, awaitable):
        """Just a helper to catch exceptions from the asyncio task
        """
        try:
            return await awaitable
        except Exception as e:
            await self.status_message("WARN", f"Task '{name}' died")
            traceback.print_exception(e)

    async def status_message(self, prefix="INFO", message=""):
        if self.status_channel is not None:
            await self.status_channel.send(f"`[{prefix}] {message}`")
        if prefix != "INFO":
            print(f"[{prefix}] {message}")

    def get_task(self, name):
        """Get the asyncio task
        """
        return self.tasks[name]

    def check_tasks(self):
        """checks if asyncio tasks are still "alive"
        """
        for name, task in self.tasks.items():
            if task is None:
                self.client.loop.create_task(self.status_message("", f"Creating task '{name}'"))
                self.tasks[name] = self.client.loop.create_task(self._log_exception(name, self.task_routines[name]()))
            elif isinstance(task, asyncio.Task):
                if task.cancelled():
                    print(f"Task '{name}' was cancelled, resetting...")
                    self.tasks[name] = None
                if task.done():
                    print(f"Task '{name}' is Done? Resetting...")
                    self.tasks[name] = None

    async def chat_destructure(self, msg: str):
        match = self.chat_regex.match(msg)
        if match is None:
            await self.status_message("WARN", f"Warning: Could not destructure chat message with Regex: {msg}")
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
                found_thread = next(filter(lambda t: t.name == name, self.channel.threads), None)
                if found_thread is not None:
                    msg = await self.channel.send(f"Found and reusing existing thread for channel: {name}")
                    self.threads[c.ID] = found_thread
                else:
                    msg = await self.channel.send(f"Creating thread for channel: {name}")
                    self.threads[c.ID] = await self.channel.create_thread(name = name, message = msg)
            return self.threads[c.ID]
        else:
            return None

    async def prune_threads(self):
        await self.channel.send("TODO: implement this :)))")

    @staticmethod
    def command_handler(cmd_fn):
        ws_commands[cmd_fn.__name__] = cmd_fn
    
    @command_handler
    async def chat(self, data: str):
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            await self.status_message("ERROR",  f"Failed to parse chat payload: {data}")
            return
        
        print(data)

        if isinstance(message, dict):
            pid: int = message['PlayerID']
            chat_id: int = message['ID']
            author: Player = self.players.get(pid, None)
            icon = (self.origin + author.image) if author and author.image else None

            content: ChatRegexGroup = await self.chat_destructure(message['Text'])

            if content is None:
                await self.status_message("WARN", f"Failed to parse chat message: {message}")
                return

            if (pid == self.server_chat_id and content.text is not None):
                if (content.text.find("LATEST CLIENT") >= 0):
                    print(f"// ------ {content.target} joined.")
                    await self.channel.send(f"**{discord.utils.escape_markdown(content.target)}** joined the server.")
                    return
                elif (found := content.text.find("Page ")) >= 0:
                    if (content.text.find("players") >= 0):
                        content.text = "Channels " + content.text[found:]
                    else:
                        content.text = "Help " + content.text[found:]
                elif content.text.count('\n') > 1:
                    lines = list(filter(lambda l: len(l.strip()) > 0, content.text.splitlines(keepends=True)))
                    content.text = lines[0] + " ... " + lines[-1]
            
            ts = None
            #ts = datetime.datetime.combine(datetime.date.today() , datetime.time.fromisoformat(content.time).replace(tzinfo=ZoneInfo("Europe/Berlin")))
            em = discord.Embed(title=f"whisper to {content.target}" if content.whisper else None, description=discord.utils.escape_markdown(content.text), timestamp = ts)
            author_name = str(pid)
            if author:
                author_name = str(author.name)
            if content.channel:
                author_name = f"[{str(content.channel)}] {author_name}"
            if pid == self.server_chat_id and content.target is not None:
                author_name += " @ " + content.target
            em.set_author(name = author_name, icon_url = icon)
            # em.add_field(name="Chat:", value=message['Text'], inline=True)
            print(f"// --------- Chat: {content.text if pid == self.server_chat_id else message['Text']}")

            target_channel_name = None
            if author and content.channel not in (None, "main"):
                print(f"Creating/getting thread for channel {content.channel}...")
                target_channel_name = content.channel
            
            tp_target_player = None
            if content.text.startswith("/tp") and author and author.channel is not None:
                tp_target_player = author
            
            if pid == self.server_chat_id and content.text.startswith(("Teleport", "Command tp")):
                tp_target_player = next(filter(lambda p: p.name == content.target, self.players.values()), None)
            
            if tp_target_player and tp_target_player.channel is not None:
                if (chan := self.get_channel_by_id(tp_target_player.channel)) is not None and chan.name not in (None, "main"):
                    target_channel_name = chan.name

            target_channel = self.channel
            if isinstance(target_channel_name, str):
                target_channel = await self.get_or_make_thread(target_channel_name)
                if target_channel is None:
                    await self.status_message("WARN", f"Failed to create/get thread for channel {target_channel_name}.")
                    target_channel = self.channel

            await self.mq.insert_or_update(chat_id, pid, em = em, channel = target_channel, purge=False if not author else (content.target == author.name and not content.text.startswith("/")))

    @command_handler
    async def update(self, data: str):
        try:
            data = json.loads(data)
        except json.decoder.JSONDecodeError:
            await self.status_message("WARN", f"Failed to parse update cmd payload: {data}.")
            return
        print(f"cmd update: {data}")
        match data:
            case str(s) if s.endswith("/players"):
                await self.get_players()
            case str(s) if s.endswith("/status"):
                await self.get_status()
            case str(s) if s.endswith("/channels"):
                await self.get_channels()
            case _:
                print(f"cmd update: {data} not implemented.")

    async def update_bot_status(self):
        self.activity.name=f"TCP / UDP: {self.status.get('TCPConnections', '?')} / {self.status.get('UDPConnections', '?')}"
        self.activity.timestamps={"start": self.status.get('StartupTime', 0)/1000}
        await self.client.change_presence(activity=self.activity)

        tcp = self.status.get('TCPConnections', 0)
        udp = self.status.get('UDPConnections', 0)
        should_ping = self.status_role is not None and tcp > 2 and udp < max(tcp * .25, 2)

        embed_age = 3600 + 1
        if (self.last_status_msg is not None and isinstance(self.last_status_msg.created_at, datetime.datetime)):
            embed_age = (datetime.datetime.now(tz=ZoneInfo("UTC")) - self.last_status_msg.created_at).total_seconds()

        time_since_update = datetime.datetime.now() - self.last_status_timestamp
        if (time_since_update.total_seconds() < 30 and not should_ping):
            return

        em = discord.Embed(
            description=dedent(f"""
                **Server Startup**: {datetime.datetime.fromtimestamp(int(self.status.get('StartupTime', 0)/1000))}
                **Player Counter**: `{self.status.get('PlayerCounter', '?')}`
                **Connections** / **TCP** / **UDP**: `{self.status.get('Connections', '?')}` / `{self.status.get('TCPConnections', '?')}` / `{self.status.get('UDPConnections', '?')}`
            """))

        if (should_ping or embed_age > 3600):
            self.last_status_msg: discord.Message = await self.status_channel.send(content=self.status_role.mention if should_ping else None, embed=em)
        else:
            self.last_status_msg: discord.Message = await self.last_status_msg.edit(content=self.status_role.mention if should_ping else f"**Last updated**: <t:{int(self.last_status_timestamp.timestamp())}:R>", embed=em)
        self.last_status_timestamp = datetime.datetime.now()

    async def api_fetch(self, endpoint: str, requests_method=requests.get, requests_data: str = None, raw: bool = False):
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
            await self.status_message("WARN", f"Failed api call {requests_method} to {endpoint} with status {response.status_code}")
            await self.status_message("WARN", f">> {response.text}")
            return None

        if raw:
            return response.text

        try:
            return response.json()
        except json.decoder.JSONDecodeError:
            await self.status_message("WARN", f"Error decoding {endpoint} response: {response.text}")
            return None

    async def get_players(self):
        """Wrapper logic around /api/players
        """
        players = await self.api_fetch("/players")
        if isinstance(players, list):
            for pdict in players:
                pid = pdict['ID']
                if pid not in self.players:
                    p = Player()
                    self.players[pid] = p
                self.players[pid].dict_update(pdict)

    async def get_status(self):
        """Wrapper logic around /api/status
        """
        self.status = await self.api_fetch("/status")
        await self.update_bot_status()

    async def get_channels(self):
        """Wrapper logic around /api/channels
        """
        channels = await self.api_fetch("/channels")
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

        auth = await self.api_fetch("/auth", requests.get)

        if isinstance(auth, dict) and 'Key' in auth:
            self.cookies['celestenet-session'] = auth['Key']
            print(f"Auth'd to {self.cookies['celestenet-session']}")
        else:
            self.cookies.pop('celestenet-session', None)
            print(f"Key not in reauth: {auth}")

        await self.get_status()
        await self.get_players()
        await self.get_channels()

        async for ws in websockets.connect(self.uri, origin=self.origin):
            if 'celestenet-session' in self.cookies:
                await ws.send("cmd")
                await ws.send("reauth")
                await ws.send(json.dumps(self.cookies['celestenet-session']))

            while True:
                try:
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
                    await self.status_message("WARN", "websocket died.")
                    break
            self.command_state = Celestenet.State.WaitForType
            self.command_current = None
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
            self.sent_in = msg.channel if msg is not None else None
            self.need_update = False
            if (self.sent() and channel != msg.channel):
                print(f"=== Warning ===\n Channel mismatch on existing msg added to queue: {channel} vs. {msg.channel}")

        def sent(self):
            return self.msg is not None and self.sent_in is not None

        async def send(self):
            if self.sent() and self.need_update:
                if self.channel == self.sent_in:
                    self.msg = await self.msg.edit(embed=self.embed)
                else:
                    print(f"Deleting message from {self.sent_in} because it belongs in {self.channel}")
                    await self.msg.delete()
                    self.msg = None
                    self.sent_in = None
                self.need_update = False
            if self.sent_in is None:
                self.msg = await self.channel.send(embed=self.embed)
                if self.msg is not None:
                    self.sent_in = self.channel
                else:
                    print(f"=== Warning ===\n Failed to send {self.embed} in {self.channel}")


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
                if drop_msg.discord.sent_in is None:
                    print("popped message before it was sent!")

    def get_by_chat_id(self, chat_id: int):
        return next(filter(lambda m: m.chat.chat_id == chat_id, self.queue), None)

    def get_by_msg_id(self, msg_id: int):
        return next(filter(lambda m: isinstance(m.discord.msg, discord.Message) and m.discord.msg.id == msg_id, self.queue), None)

    async def insert_or_update(self, chat_id: int, user_id: int, em: discord.Embed, msg: discord.Message = None, channel: discord.TextChannel = None, purge: bool = False):
        async with self.lock:
            found_msg = self.get_by_chat_id(chat_id)

            # print(f"Checking for {chat_id}: {found_msg}")

            if isinstance(found_msg, MessageQueue.Message):
                if purge:
                    print(f"Purging from MQ: {found_msg.chat.chat_id} {user_id}")
                    self.queue.remove(found_msg)
                    return
                found_msg.chat.user = user_id
                found_msg.discord.embed = em
                found_msg.discord.msg = msg
                found_msg.discord.channel = channel
                found_msg.discord.need_update = True
                return

            new_msg = MessageQueue.Message(
                MessageQueue.ChatMsg(chat_id, user_id),
                MessageQueue.DiscordMsg(em, msg, channel)
            )

            #print(f"[{MessageQueue.timestamp()}] Inserting into MQ: {new_msg.recv_time} {new_msg.chat.chat_id} {new_msg.discord.channel}")
            self.queue.insert(0, new_msg)

    async def process(self):
        async with self.lock:
            for m in reversed(self.queue):
                if m.discord.need_update or (not m.discord.sent() and MessageQueue.timestamp() - m.recv_time > self.delay_before_send):
                    await m.discord.send()