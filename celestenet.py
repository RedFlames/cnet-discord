import datetime
import random
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
        self.source: str | None = d.get('source', None)
        self.target: str | None = d.get('target', None)
        self.text: str | None = d.get('text', None)

ws_commands = {}

class CelestenetListener:
    def __init__(self, chat_channel: discord.abc.GuildChannel, status_channel: discord.abc.GuildChannel, status_role: discord.Role):
        self.chat_channel: discord.abc.GuildChannel = chat_channel
        self.status_channel: discord.abc.GuildChannel = status_channel
        self.status_role: discord.Role = status_role
        self.mq: MessageQueue = MessageQueue(status_role = status_role)
        self.last_status_msg: discord.Message = None
        self.last_status_timestamp = datetime.datetime.now() - datetime.timedelta(minutes=5)
        self.last_ping_timestamp = datetime.datetime.now() - datetime.timedelta(hours=5)
        self.threads: dict[int, discord.Thread] = {}

    async def get_or_make_thread(self, channel:str, cid: int):
        if cid not in self.threads:
            found_thread = next(filter(lambda t: t.name == channel, self.chat_channel.threads), None)
            if found_thread is not None:
                msg = await self.chat_channel.send(f"Found and reusing existing thread for channel: {channel}")
                self.threads[cid] = found_thread
            else:
                msg = await self.chat_channel.send(f"Creating thread for channel: {channel}")
                self.threads[cid] = await self.chat_channel.create_thread(name = channel, message = msg)
        return self.threads[cid]

    async def status_message(self, prefix="INFO", message=""):
        if self.status_channel is not None:
            await self.status_channel.send(f"`[{prefix}] {message}`")

    async def update_status(self, em: discord.Embed, should_ping, ping_msg: str | None = None):
        
        embed_age = 3600 + 1
        if (self.last_status_msg is not None and isinstance(self.last_status_msg.created_at, datetime.datetime)):
            embed_age = (datetime.datetime.now(tz=ZoneInfo("UTC")) - self.last_status_msg.created_at).total_seconds()

        time_since_update = datetime.datetime.now() - self.last_status_timestamp
        if (time_since_update.total_seconds() < 30 and not should_ping):
            return

        time_since_ping = datetime.datetime.now() - self.last_ping_timestamp
        if should_ping and time_since_ping.total_seconds() > 3600:
            self.last_ping_timestamp = datetime.datetime.now()
            await self.status_channel.send(content=self.status_role.mention, allowed_mentions=discord.AllowedMentions(everyone=False, roles=True))
            await self.status_channel.send(content=f"{ping_msg}")

        if embed_age > 3600:
            self.last_status_msg: discord.Message = await self.status_channel.send(content=None, embed=em)
        else:
            self.last_status_msg: discord.Message = await self.last_status_msg.edit(content=f"**Last updated**: <t:{int(self.last_status_timestamp.timestamp())}:R>", embed=em)
        self.last_status_timestamp = datetime.datetime.now()

    async def prune_threads(self, days_old = 14):
        await self.status_channel.send(content=f"There are {len(self.chat_channel.threads)} in {self.chat_channel.mention}...")
        for t in self.chat_channel.threads:
            if t.message_count == 0:
                await self.status_channel.send(content=f"There are no messages in {t.name}, deleting...")
                await t.delete()
                continue
            try:
                m = await t.fetch_message(t.last_message_id)
            except discord.NotFound:
                await self.status_channel.send(content=f"Failed to fetch last message of {t.name}, moving on...")
                continue
            if m is not None and isinstance(m.created_at, datetime.datetime):
                if (datetime.datetime.now(tz=ZoneInfo("UTC")) - m.created_at).days > days_old:
                    await self.status_channel.send(content=f"Last message in {t.name} is older than {days_old} days, deleting...")
                    await t.delete()
                continue
        await self.status_channel.send(content=f"There are {len(self.chat_channel.threads)} in {self.chat_channel.mention} left after cleanup. Iterating archived threads...")
        async for t in self.chat_channel.archived_threads():
            if t.message_count == 0:
                await self.status_channel.send(content=f"There are no messages in {t.name}, deleting...")
                await t.delete()
                continue
            try:
                m = await t.fetch_message(t.last_message_id)
            except discord.NotFound:
                await self.status_channel.send(content=f"Failed to fetch last message of {t.name}, moving on...")
                continue
            if m is not None and isinstance(m.created_at, datetime.datetime):
                if (datetime.datetime.now(tz=ZoneInfo("UTC")) - m.created_at).days > days_old:
                    await self.status_channel.send(content=f"Last message in {t.name} is older than {days_old} days, deleting...")
                    await t.delete()
                continue
        await self.status_channel.send(content=f"There are {len(self.chat_channel.threads)} in {self.chat_channel.mention} left.")

    async def process(self):
            await self.mq.prune()
            await self.mq.process()

class ExponentialBackoff:
    def __init__(self, min_seconds: int, max_seconds: int, reset_seconds: int, min_count: int = 0):
        self.last_attempt = time.time()
        self.last_success = time.time()
        self.backoff_min = min_seconds
        self.backoff_max = max_seconds
        self.backoff_reset = reset_seconds
        self.min_count = min_count
        self.attempts_total = 0
        self.attempts_backoff = 0
        self.current_backoff = 0

    @property
    def excess(self) -> int:
        return self.attempts_total - self.min_count

    def retry(self):
        self.attempts_total += 1
        self.last_attempt = time.time()

        self.current_backoff = min(self.backoff_min + pow(2, self.attempts_backoff - self.min_count), self.backoff_max)
        if (self.excess > 0) and (time.time() - self.last_success) < self.current_backoff:
            return False
        
        self.attempts_backoff += 1
        self.last_success = time.time()
        return True

    def update(self):
        if (time.time() - self.last_attempt) > self.backoff_reset:
            self.reset()
            return True
        return False

    def reset(self):
        self.attempts_total = 0
        self.attempts_backoff = 0



class TaskWrapper:
    def __init__(self, name: str, awaitable, wrapper = None):
        self.awaitable = awaitable
        self.wrapper = wrapper
        self.task: asyncio.Task = None
        self.name: str = name

        self.backoff = ExponentialBackoff(0, 1800, 30)

    def retry(self, loop: asyncio.AbstractEventLoop):
        if self.backoff.retry():
            self.task = loop.create_task(self.wrapper(self.name, self.awaitable()) if self.wrapper else self.awaitable())
            print(f"Task {self.name} after retry: {self.task}")
            return True
        return False
    
    def update(self):
        self.backoff.update()

        if self.task is None:
            return TaskWrapper.State.Unknown

        if self.task.cancelled():
            return TaskWrapper.State.Cancelled

        if self.task.done():
            return TaskWrapper.State.Stopped

        return TaskWrapper.State.Started

    class State(Enum):
        Unknown = -1
        Stopped = auto()
        Started = auto()
        Cancelled = auto()

class RateLimiter:
    def __init__(self, win_size: float, lim: int):
        self.window_size: float = win_size
        self.next_window: float = time.time() + win_size + self.jitter()
        self.attempts: int = 0
        self.last_excess: int = 0
        self.limit: int = lim

    def jitter(self, size: float = None):
        if size is None:
            size = self.window_size * .33
        return random.uniform(0.0, size)

    def limit_me(self):
        self.update()
        
        self.attempts += 1

        if self.attempts < self.limit:
            return False
        return True
    
    def pop_last_excess(self):
        out = self.last_excess
        self.last_excess = 0
        return out

    def update(self):
        if time.time() > self.next_window:
            self.next_window = time.time() + self.window_size + self.jitter()
            if self.attempts >= self.limit:
                self.last_excess = self.attempts
            self.attempts = 0

class Celestenet:
    def __init__(self):
        """Handles interactions with CN's JSON API and websocket
        """
        self.uri: str = 'wss://celestenet.0x0a.de/api/ws'
        self.origin: str = 'https://celestenet.0x0a.de'
        self.api_base: str = self.origin + '/api'
        self.api_limiter: RateLimiter = RateLimiter(10.0, 20)

        self.socket_task = TaskWrapper("socket", self.socket_relay, self._log_exception)
        self.process_task = TaskWrapper("process", self.process, self._log_exception)

        self.players: dict[int, Player] = {}
        self.channels: dict[int, Channel] = {}
        
        self.command_state: Celestenet.State = Celestenet.State.WaitForType
        self.command_current = None
        self.status: dict[str, Any] = {}
        self.activity: discord.Activity = discord.Activity(
                        name="Celestenet",
                        type=discord.ActivityType.watching,
                        details="Starting...", timestamps={"start": datetime.datetime.now().timestamp})

        self.chat_regex = re.compile(r'(?sm)^\[(?P<time>[0-9:]+)\] (?: ?\[(?P<whisper>whisper)[^]]*\])? ?(?: ?\[channel (?P<channel>[^]]+)\])? ?(?::celestenet_avatar_[0-9]+_: )?(?P<source>[^ ]+)(?: @ (?::celestenet_avatar_[0-9]+_: )?(?P<target>[^:]+))?: ?(?P<text>.*)$')
        self.avatar_regex = re.compile(r':celestenet_avatar_[0-9]+_: ?')

        """ these three get set in init_client(...) below """
        self.client: discord.Client = None
        self.cookies: dict = {}

        self.recipients: list[CelestenetListener] = []

        self.server_chat_id = (1 << 32) - 1
        self.players[self.server_chat_id] = Player(self.server_chat_id, "** SERVER **")
        self.phrases: list[str] = []

        self.ping_on_next_status = False
        self.last_status_update = time.time()

    async def init_client(self, client: discord.Client, cookies: dict | str):
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
        if isinstance(cookies, str):
            self.cookies = json.loads(cookies)
        elif isinstance(cookies, dict):
            self.cookies = cookies

    async def load_phrases(self, file: str):
        self.phrases = []
        try:
            with open(file) as f:
                phrases = json.load(f)
            for p in phrases:
                print(f"Building regex {p}")
                self.phrases.append(re.compile(p))
            return f"Successfully loaded {file}."
        except Exception as e:
             return traceback.print_exception(e)

    async def add_recipient(self, chat_channel: discord.abc.GuildChannel, status_channel: discord.abc.GuildChannel, status_role: discord.Role):
        try:
            await chat_channel.send("Testing chat channel send...")
            await status_channel.send("Testing status channel send...")
            self.recipients.append(CelestenetListener(chat_channel, status_channel, status_role))
            return "Successfully added listener channels."
        except Exception as e:
             return traceback.print_exception(e)

    async def _log_exception(self, name, awaitable):
        """Just a helper to catch exceptions from the asyncio task
        """
        try:
            return await awaitable
        except Exception as e:
            await self.status_message("WARN", f"Task '{name}' died")
            traceback.print_exception(e)

    async def status_message(self, prefix="INFO", message=""):
        for rec in self.recipients:
            await rec.status_message(prefix, message)
        if prefix != "INFO":
            print(f"[{prefix}] {message}")

    def update_tasks(self):
        """checks if asyncio tasks are still "alive"
        """
        for task in (self.socket_task, self.process_task):
            status = task.update()

            if status in (TaskWrapper.State.Cancelled, TaskWrapper.State.Stopped) and task.task is not None:
                self.client.loop.create_task(self.status_message("", f"Task failed with status {status}:\n'{task.task.exception() if status == TaskWrapper.State.Stopped else ''}'"))

            if status != TaskWrapper.State.Started:
                if task.retry(self.client.loop):
                    self.client.loop.create_task(self.status_message("", f"Created task '{task.name}' (queries/attempts/backoff: {task.backoff.attempts_total} / {task.backoff.attempts_backoff} / {task.backoff.current_backoff}s)"))

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

    async def to_chat_channels(self, msg: str):
        for rec in self.recipients:
            await rec.chat_channel.send(msg)

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
        
        print(data.replace('\n',' '))

        if isinstance(message, dict):
            pid: int = message['PlayerID']
            chat_id: int = message['ID']
            author: Player = self.players.get(pid, None)
            icon = (self.origin + author.image) if author and author.image else None

            content: ChatRegexGroup = await self.chat_destructure(message['Text'])
            discord_message_text: str = None
            naughty_word: str = None
            show_embed: bool = True
            check_name: bool = False

            if content is None:
                await self.status_message("WARN", f"Failed to parse chat message: {message}")
                return

            if content.text:
                content.text = self.avatar_regex.sub('', content.text)

            if (pid == self.server_chat_id and content.text is not None):
                if (content.text.find("LATEST CLIENT") >= 0):
                    print(f"// ------ {content.target} joined.")
                    discord_message_text = f"**{discord.utils.escape_markdown(content.target)}** joined the server."
                    show_embed = False
                    check_name = True
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
            em = None
            if show_embed:
                em = discord.Embed(description=discord.utils.escape_markdown(content.text), timestamp = ts)
            author_name = str(pid)
            if author:
                author_name = str(author.name)
            elif content.source:
                author_name = content.source
            if content.channel:
                author_name = f"[{str(content.channel)}] {author_name}"
            if pid == self.server_chat_id and content.target is not None:
                author_name += " @ " + content.target
            if em is not None:
                em.set_author(name = author_name, icon_url = icon)
            # em.add_field(name="Chat:", value=message['Text'], inline=True)
            print(f"// --------- Chat: {content.text if pid == self.server_chat_id else message['Text']}")

            if content.whisper:
                discord_message_text = f"Whisper {author_name} @ {content.target}: ||{discord.utils.escape_markdown(content.text)}||"
                em = None

            target_channel_name = None
            if author and content.channel not in (None, "main"):
                print(f"Creating/getting thread for channel {content.channel}...")
                target_channel_name = content.channel
            
            if content.text.startswith("/") and author and pid != self.server_chat_id:
                discord_message_text = f"**{author_name}**: `{discord.utils.escape_markdown(content.text)}`"
                em = None

            tp_target_player = None
            if content.text.startswith("/tp") and author and author.channel is not None:
                tp_target_player = author
            
            if pid == self.server_chat_id and content.text.startswith(("Teleport", "Command tp")):
                tp_target_player = next(filter(lambda p: p.name == content.target, self.players.values()), None)
                discord_message_text = f"{author.name} @ {discord.utils.escape_markdown(content.target)}: `{discord.utils.escape_markdown(content.text)}`"
                em = None

            if pid == self.server_chat_id and content.text.startswith(("Moved to", "Already in")):
                discord_message_text = f"{author.name} @ {discord.utils.escape_markdown(content.target)}: `{discord.utils.escape_markdown(content.text)}`"
                em = None

            if tp_target_player and tp_target_player.channel is not None:
                if (chan := self.get_channel_by_id(tp_target_player.channel)) is not None and chan.name not in (None, "main"):
                    target_channel_name = chan.name
            if content.channel in ("main", None) and content.target is None:
                for p in self.phrases:
                    m = p.search(content.text if not check_name else content.target)
                    if m is not None:
                        naughty_word = m.group(0)
                        if discord_message_text is None:
                            discord_message_text = naughty_word
                        else:
                            discord_message_text = f"{discord_message_text} ({naughty_word})"
                        break

            for rec in self.recipients:
                rec_target_channel = rec.chat_channel
                if isinstance(target_channel_name, str):
                    target_channel_cnet: Channel = self.get_channel_by_name(target_channel_name)
                    if target_channel_cnet:
                        rec_target_channel = await rec.get_or_make_thread(target_channel_name, target_channel_cnet.ID)
                    if rec_target_channel is None:
                        await rec.status_message("WARN", f"Failed to create/get thread for channel {target_channel_name}.")
                        rec_target_channel = rec.chat_channel

                await rec.mq.insert_or_update(chat_id, pid, content = discord_message_text, em = em, channel = rec_target_channel, purge=False if not author else (content.target == author.name and not content.text.startswith("/")), ping = naughty_word is not None)

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
        if self.status is None or not isinstance(self.status, dict):
            print(f"Unknown status: {self.status}.")
            return

        self.activity.name=f"TCP/UDP: {self.status.get('TCPConnections', '?')}/{self.status.get('UDPConnections', '?')} ({len(asyncio.all_tasks(self.client.loop))})"
        self.activity.timestamps={"start": self.status.get('StartupTime', 0)/1000}
        await self.client.change_presence(activity=self.activity)

        tcp = self.status.get('TCPConnections', 0)
        udp = self.status.get('UDPConnections', 0)
        should_ping = tcp > 2 and udp < max(tcp * .25, 2)

        em = discord.Embed(
            description=dedent(f"""
                **Server Startup**: {datetime.datetime.fromtimestamp(int(self.status.get('StartupTime', 0)/1000))}
                **Player Counter**: `{self.status.get('PlayerCounter', '?')}`
                **Connections** / **TCP** / **UDP**: `{self.status.get('Connections', '?')}` / `{self.status.get('TCPConnections', '?')}` / `{self.status.get('UDPConnections', '?')}`
            """
        ))
        
        for rec in self.recipients:
            await rec.update_status(em, should_ping, self.activity.name)

    async def prune_threads(self, days_old = 14):
        for rec in self.recipients:
            await rec.prune_threads(days_old)

    async def api_fetch(self, endpoint: str, requests_method=requests.get, requests_data: str = None, raw: bool = False):
        if self.api_limiter.limit_me():
            return None
        
        excess = self.api_limiter.pop_last_excess()
        if excess > self.api_limiter.limit * 2:
            await self.status_message("WARN", f"Too many API calls in last window! (allowed {self.api_limiter.limit} of {excess} in {self.api_limiter.window_size}s window)")

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
        self.last_status_update = time.time()
        self.status = await self.api_fetch("/status")
        if self.ping_on_next_status:
            self.ping_on_next_status = False
            if self.status and 'UDPConnections' in self.status:
                self.status['UDPConnections'] = 0
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
            try:
                for rec in self.recipients:
                    await rec.process()
                
                if time.time() - self.last_status_update > 60:
                    await self.get_status()
            
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                print("Process task received CancelledError. Exiting.")
                return

    async def socket_relay(self):
        """Async task that
            - tries to auth with cookies and such
            - opens and re-opens websocket connection with an endless loop
            - attempts auth against WS too
            - handles the various WS commands
            - sends discord messages like relaying the chat log
        """
        print("Relay: Awaiting discord client ready...")
        await self.client.wait_until_ready()
        print("Client ready.")

        auth = await self.api_fetch("/auth", requests.get)

        if isinstance(auth, dict) and 'Key' in auth:
            self.cookies['celestenet-session'] = auth['Key']
            await self.status_message(f"Auth: {auth['Info']}")
        else:
            self.cookies.pop('celestenet-session', None)
            await self.status_message(f"Key not in reauth: {auth}")

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
                except asyncio.CancelledError:
                    print("Socket_relay received CancelledError. Exiting.")
                    return
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
        def __init__(self, content: str = "", em: discord.Embed = None, msg: discord.Message = None, channel: discord.TextChannel = None, ping: discord.Role = None):
            self.embed = em
            self.ping = ping
            self.content = content if ping is None else f"{content} {self.ping.mention}"
            self.msg = msg
            self.channel = channel
            self.sent_in = msg.channel if msg is not None else None
            self.need_update = False
            if (self.sent() and channel != msg.channel):
                print(f"=== Warning ===\n Channel mismatch on existing msg added to queue: {channel} vs. {msg.channel}")

        def update(self, content: str = "", em: discord.Embed = None, msg: discord.Message = None, channel: discord.TextChannel = None, ping: discord.Role = None):
            self.embed = em
            self.ping = ping
            self.content = content if ping is None else f"{content} {self.ping.mention}"
            self.msg = msg
            self.channel = channel
            self.need_update = True

        def sent(self):
            return self.msg is not None and self.sent_in is not None

        async def send(self):
            if self.sent() and self.need_update:
                if self.channel == self.sent_in:
                    self.msg = await self.msg.edit(content=self.content, embed=self.embed)
                else:
                    print(f"Deleting message from {self.sent_in} because it belongs in {self.channel}")
                    await self.msg.delete()
                    self.msg = None
                    self.sent_in = None
                self.need_update = False
            if self.sent_in is None:
                self.msg = await self.channel.send(content=self.content, embed=self.embed, allowed_mentions=discord.AllowedMentions(everyone=False, roles=True) if self.ping else None)
                if self.msg is not None:
                    self.sent_in = self.channel
                else:
                    print(f"=== Warning ===\n Failed to send {self.content} ({self.embed}) in {self.channel}")


    class Message:
        def __init__(self, chat, discord):
            self.recv_time = MessageQueue.timestamp()
            self.chat: MessageQueue.ChatMsg = chat
            self.discord: MessageQueue.DiscordMsg = discord

    def timestamp():
        return int( time.time_ns() / 1000 )

    def __init__(self, max_len: int = 40, delay_before_send: int = 500, status_role: discord.Role = None):
        self.max_len = max_len
        self.delay_before_send = delay_before_send
        self.queue: list[MessageQueue.Message] = []
        self.lock = asyncio.Lock()
        self.status_role = status_role

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

    async def insert_or_update(self, chat_id: int, user_id: int, content: str = None, em: discord.Embed = None, msg: discord.Message = None, channel: discord.TextChannel = None, purge: bool = False, ping: bool = False):
        async with self.lock:
            found_msg = self.get_by_chat_id(chat_id)

            print(f"Checking for {chat_id}: {found_msg}")

            if isinstance(found_msg, MessageQueue.Message):
                if purge:
                    print(f"Purging from MQ: {found_msg.chat.chat_id} {user_id}")
                    self.queue.remove(found_msg)
                    return
                found_msg.chat.user = user_id
                found_msg.discord.update(content, em, found_msg.discord.msg if msg is None else msg, channel, self.status_role if ping else None)
                return

            new_msg = MessageQueue.Message(
                MessageQueue.ChatMsg(chat_id, user_id),
                MessageQueue.DiscordMsg(content, em, msg, channel, self.status_role if ping else None)
            )

            #print(f"[{MessageQueue.timestamp()}] Inserting into MQ: {new_msg.recv_time} {new_msg.chat.chat_id} {new_msg.discord.channel}")
            self.queue.insert(0, new_msg)

    async def process(self):
        async with self.lock:
            for m in reversed(self.queue):
                if m.discord.need_update or (not m.discord.sent() and MessageQueue.timestamp() - m.recv_time > self.delay_before_send):
                    await m.discord.send()
