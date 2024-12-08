import datetime
import os
import random
from zoneinfo import ZoneInfo
from enum import Enum, auto
import re
import time
import traceback
from typing import Any, Optional
import asyncio
import json
import discord
import websockets
import requests
from textwrap import dedent

strip_chars = ('\xad', '\xa0')

class Player:
    def __init__(self, ID=0, name='', image=None):
        self.ID = ID
        self.UID = ''
        self.name = name
        for char in strip_chars:
            self.name = self.name.replace(char, '')

        self.image = image
        self.channel = None

    def dict_update(self, pd: dict):
        self.ID = pd.get('ID', self.ID)
        self.UID = pd.get('UID', self.UID)
        self.name = pd.get('FullName', self.name)
        for char in strip_chars:
            self.name = self.name.replace(char, '')
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
    def __init__(self, chat_channel: discord.abc.GuildChannel, status_channel: discord.abc.GuildChannel, status_role: discord.Role, use_threads: bool):
        self.chat_channel: discord.abc.GuildChannel = chat_channel
        self.status_channel: discord.abc.GuildChannel = status_channel
        self.status_role: discord.Role = status_role
        self.mq: MessageQueue = MessageQueue(status_role = status_role)
        self.last_status_msg: discord.Message = None
        self.last_status_timestamp = datetime.datetime.now() - datetime.timedelta(minutes=5)
        self.last_ping_timestamp = datetime.datetime.now() - datetime.timedelta(hours=5)
        self.use_threads = use_threads
        self.threads: dict[int, discord.Thread] = {}

    async def get_or_make_thread(self, channel:str, cid: int):
        if self.use_threads is False:
            return None
        if cid not in self.threads:
            found_thread = next(filter(lambda t: t.name == channel, self.chat_channel.threads), None)
            if found_thread is not None:
                msg = await self.chat_channel.send(f"Found and reusing existing thread for channel: {channel}")
                self.threads[cid] = found_thread
            else:
                msg = await self.chat_channel.send(f"Creating thread for channel: {channel}")
                self.threads[cid] = await self.chat_channel.create_thread(name = channel, message = msg)
        return self.threads[cid]

    async def status_message(self, prefix="INFO", message="", coded=True):
        if self.status_channel is not None:
            await self.status_channel.send(f"`[{prefix}] {message}`" if coded else f"**[{prefix}]** {message}")

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

class AutoRestartMetrics:
    def __init__(self, player_limit_low: int, player_limit_high: int, player_limit_step_up: int, limit_step_up_window_h: float, days_cycle: int, force_at_limit: bool):
        self._is_active: bool = True
        self.player_limit_low: int = player_limit_low
        self.player_limit_high: int = player_limit_high
        self.player_limit_current: int = player_limit_low
        self.player_limit_step_up: int = player_limit_step_up
        self.limit_step_up_window: float = limit_step_up_window_h * 3600
        self.cooldown: float = days_cycle * 24 * 3600

        self.next_possible_restart: float = time.time() + self.cooldown
        self.exceeded_cooldown: bool = False
        self.exceeded_cooldown_at: float|None = None
        self.reset_cooldown()

        self.next_step_up_at: float = time.time() + self.limit_step_up_window
        self.force_at_limit = force_at_limit

        self.initial_start_time: datetime.datetime = None
        self.initial_start_delaying: bool = False
        self.set_initial_start(8)

    def set_initial_start(self, hour_of_day: int = None):
        self.reset()
        if hour_of_day:
            self.initial_delay_to_hour: int = hour_of_day
        self.initial_start_time = datetime.datetime.now(datetime.timezone.utc)
        if self.initial_start_time.hour >= self.initial_delay_to_hour:
            self.initial_start_time += datetime.timedelta(days=1)
        self.initial_start_time = self.initial_start_time.replace(hour=self.initial_delay_to_hour, minute=0)
        self.initial_start_delaying = True

    @property
    def is_active(self):
        return self._is_active
    
    @is_active.setter
    def is_active(self, value):
        if not self._is_active and value:
            self.set_initial_start()
        self._is_active = value

    def toggle_active(self):
        self.is_active = not self._is_active

    def set_days_cycle(self, number_of_days: int):
        self.cooldown: float = number_of_days * 24 * 3600

    def reset_cooldown(self):
        self.next_possible_restart: float = time.time() + self.cooldown
        self.exceeded_cooldown: bool = False
        self.exceeded_cooldown_at: float|None = None

    def update(self) -> bool:
        if not self.is_active:
            return False

        if self.initial_start_delaying:
            if time.time() > self.initial_start_time.timestamp():
                self.initial_start_delaying = False
                self.reset_cooldown()
            else:
                return False

        if time.time() > self.next_possible_restart and not self.exceeded_cooldown:
            self.exceeded_cooldown = True
            self.next_step_up_at: float = time.time() + self.limit_step_up_window
            self.exceeded_cooldown_at: float|None = time.time()
        
        if self.exceeded_cooldown:
            if time.time() > self.next_step_up_at and self.step_up():
                return True
        return False
        
    def step_up(self):
        if self.player_limit_current < self.player_limit_high:
            self.player_limit_current += self.player_limit_step_up
        elif self.player_limit_current == self.player_limit_high and not self.force_at_limit:
            old_exc_cd = self.exceeded_cooldown_at
            self.reset()
            self.next_possible_restart = old_exc_cd + 24 * 3600
            return False
        self.next_step_up_at = time.time() + self.limit_step_up_window
        return True

    def reset(self):
        self.player_limit_current = self.player_limit_low
        self.reset_cooldown()
    
    def should_restart(self, player_count: int) -> bool:
        if not self.is_active:
            return False
        if self.exceeded_cooldown and player_count <= self.player_limit_current:
            return True
        if self.force_at_limit and self.player_limit_current == self.player_limit_high:
            return True
        return False


class WsQueueCmd:
    def __init__(self, cmd: str, data):
        self.cmd = cmd
        self.data = data
        self.result = None

class Celestenet:
    def __init__(self):
        """Handles interactions with CN's JSON API and websocket
        """
        self.uri: str = 'wss://celestenet.0x0a.de/api/ws'
        self.origin: str = 'https://celestenet.0x0a.de'
        self.api_base: str = self.origin + '/api'
        self.api_limiter: RateLimiter = RateLimiter(10.0, 20)
        self.backoff_auth: ExponentialBackoff = ExponentialBackoff(0, 3600, 30)

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.ws_queue: dict[int, WsQueueCmd] = {}
        self.ws_queue_max: int = 5
        self.ws_queue_next: int = 0

        self.socket_task = TaskWrapper("socket", self.socket_relay, self._log_exception)
        self.process_task = TaskWrapper("process", self.process, self._log_exception)

        self.players: dict[int, Player] = {}
        self.channels: dict[int, Channel] = {}
        self.pinged_for_name: dict[int, int] = {}
        
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
        self.ws_needs_reauth = False

        self.recipients: list[CelestenetListener] = []
        self.rec_file: str = ''

        self.server_chat_id = (1 << 32) - 1
        self.players[self.server_chat_id] = Player(self.server_chat_id, "** SERVER **")
        self.phrases: list[str] = []

        self.emote_replace: dict[str, str] = {}

        self.ping_on_next_status = False
        self.last_status_update = time.time()

        """ since I made a message ID kinda required, just gonna go into negative numbers with these... """
        self.imaginary_chat_id: int = 0

        # With a cooldown of 24 hours, auto-restart when below 10 players, or
        # when cooldown exceeded, raise player threshold by 5 every 5 hours.
        # e.g. if never below 10, but 
        # <= 15 players at 29h -> restart
        # <= 20 players at 34h -> restart [...]
        # up to <= 40 player restart at 55h (seems unlikely that we will not dip below this or earlier limits in the time frame.)
        self.auto_restart_metrics: AutoRestartMetrics = AutoRestartMetrics(10, 35, 5, 1.0, 2, False)

        self.sched_restart: bool = False
        self.sched_res_init_at: int = 0
        self.sched_res_delay: int = 0
        self.sched_last_notif_at: int = 0
        self.sched_next_notif_at: int = 0

        self.restarter_failsafe_tripped: bool = False
        self.restarter_failsafe_counter: int = 0
        self.restarter_last_restart: int = time.time()

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

    async def set_emote_replaces(self, emote_replace: dict[str, str]):
        for k, v in emote_replace.items():
            self.emote_replace[k] = v

    async def load_phrases(self, file: str):
        self.phrases = []
        try:
            with open(file, encoding="utf-8") as f:
                phrases = json.load(f)
            for p in phrases:
                print(f"Building regex {p}")
                self.phrases.append(re.compile(p))
            return f"Successfully loaded {file}."
        except Exception as e:
            return traceback.print_exception(e)

    async def save_recipients(self, file: str|None = None):
        if file is None:
            file = self.rec_file
        if file is None:
            print(f"Couldn't save recipients to '{file}' / '{self.rec_file}'")
        list_out = []
        for r in self.recipients:
            #chat_channel: discord.abc.GuildChannel, status_channel: discord.abc.GuildChannel, status_role: discord.Role
            list_out.append({"guild": r.status_channel.guild.id, "chat": r.chat_channel.id, "status": r.status_channel.id, "role": r.status_role.id, "threads-non-main": r.use_threads})
        with open(self.rec_file, "w", encoding="utf-8") as f:
            json.dump(list_out, f)
        print(f"Successfully saved recipients to {self.rec_file}.")

    async def load_recipients(self, file: str):
        self.rec_file = file
        self.recipients = []
        try:
            recs = []
            with open(self.rec_file, encoding="utf-8") as f:
                recs = json.load(f)
            for r in recs:
                print(f"Adding recipient {r}")
                guild: discord.Guild = self.client.get_guild(r["guild"])
                threads_non_main = r.get("threads-non-main")
                await self.add_recipient(guild.get_channel(r["chat"]), guild.get_channel(r["status"]), guild.get_role(r["role"]), threads_non_main is True)
            return f"Successfully loaded {self.rec_file}."
        except Exception as e:
            return traceback.print_exception(e)

    async def add_recipient(self, chat_channel: discord.abc.GuildChannel, status_channel: discord.abc.GuildChannel, status_role: discord.Role, use_threads: bool):
        try:
            await chat_channel.send("Testing chat channel send...")
            await status_channel.send("Testing status channel send...")
            self.recipients.append(CelestenetListener(chat_channel, status_channel, status_role, use_threads))
            await self.save_recipients()
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

    async def status_message(self, prefix="INFO", message="", coded=True):
        for rec in self.recipients:
            await rec.status_message(prefix, message, coded)
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

    async def queue_wscmd(self, cmd: str, data):
        if len(self.ws_queue) >= self.ws_queue_max:
            return -1
        self.ws_queue_next += 1
        data = json.dumps(data)
        print(f"Sending wscmd #{self.ws_queue_next}: {cmd} with data {data}")
        await self.ws.send("cmd")
        await self.ws.send(cmd)
        await self.ws.send(data)
        self.ws_queue[self.ws_queue_next] = WsQueueCmd(cmd, data)
        return self.ws_queue_next

    async def invoke_wscmd(self, cmd: str, data):
        i: int = await self.queue_wscmd(cmd, data)
        if i < 0:
            return "Unable to queue more ws cmds."
        while i in self.ws_queue and self.ws_queue[i].result is None:
            print(f"Command {i} awaiting response data...")
            await asyncio.sleep(1)
        queue_item = self.ws_queue.pop(i, None)
        if queue_item is None:
            return "Could not retrieve response."
        return queue_item.result

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
            icon = (f"{self.origin}{author.image}&time={datetime.date.today()}") if author and author.image else None

            #content: ChatRegexGroup = await self.chat_destructure(message['Text'])
            content: ChatRegexGroup = ChatRegexGroup(
                     {
                          "time": message.get("DateTime", None),
                          "channel": message.get("Tag", None),
                          "whisper": (message.get("Targets", None) != None) and (message.get("Tag", None) == "whisper"),
                          "source": author.name if author else pid,
                          "target": message.get("Targets", None),
                          "text": message.get("Text", None)
                     }
            )

            if content.channel != None and content.channel.startswith("channel "):
                content.channel = content.channel[8:]
            if content.channel != None and len(content.channel.strip()) == 0:
                content.channel = None
            if isinstance(content.target, list) and len(content.target) > 0:
                if len(content.target) == 1 and content.target[0] == pid and not content.whisper and not content.text.startswith("/"):
                    print(f"// Dropping broadcast echo {pid} == {content.target}.")
                    return
                content.target = content.target[0]
            if isinstance(content.target, int):
                if content.target in self.players:
                    content.target = self.players[content.target].name
                else:
                    content.target = str(content.target)

            if content.text.lower().startswith(("/w ", "/whisper", "/cc", "/channelchat")):
                print("// Dropping whisper/cc.")
                return
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
                if (content.text.find("Welcome to the CelesteNet") >= 0) or (content.text.find("Welcome to CelesteNet") >= 0) or (content.text.find("Welcome to the official CelesteNet") >= 0):
                    print(f"// ------ {content.target} joined. (ignoring MOTD whisper)")
                    return
                elif (found := content.text.find("Page ")) >= 0:
                    if (content.text.find("players") >= 0):
                        content.text = "Channels " + content.text[found:]
                    else:
                        content.text = "Help " + content.text[found:]
                elif content.text.count('\n') > 1:
                    lines = list(filter(lambda l: len(l.strip()) > 0, content.text.splitlines(keepends=True)))
                    content.text = lines[0] + " ... " + lines[-1]

            if not content.text.startswith("/"):
                for k, v in self.emote_replace.items():
                    content.text = content.text.replace(k, v)

            ts = None
            #ts = datetime.datetime.combine(datetime.date.today() , datetime.time.fromisoformat(content.time).replace(tzinfo=ZoneInfo("Europe/Berlin")))
            em = None
            if show_embed:
                em = discord.Embed(description=discord.utils.escape_markdown(content.text), timestamp = ts)
            author_name = str(pid)
            if author:
                author_name = str(author.name)
            elif content.source:
                author_name = str(content.source)
            if content.channel:
                author_name = f"[{str(content.channel)}] {author_name}"
            if pid == self.server_chat_id and content.target is not None:
                author_name += " @ " + content.target
            for char in strip_chars:
                author_name = author_name.replace(char, '')
            if em is not None:
                em.set_author(name = author_name, icon_url = icon)
            # em.add_field(name="Chat:", value=message['Text'], inline=True)
            print(f"// --------- Chat: {content.text if pid == self.server_chat_id else message['Text']}")

            if content.whisper:
                discord_message_text = f"Whisper {author_name} @ {content.target}: ||{discord.utils.escape_markdown(content.text)}||"
                em = None

            msg_lower = content.text.lower()

            target_channel_name = None
            if author and content.channel not in (None, "main") and not content.whisper:
                print(f"Creating/getting thread for channel {content.channel}...")
                target_channel_name = content.channel
            
            if content.text.startswith("/") and author and pid != self.server_chat_id:
                discord_message_text = f"**{author_name}**: `{discord.utils.escape_markdown(content.text)}`"
                em = None

            tp_target_player = None
            if msg_lower.startswith(("/tp","/e")) and author and author.channel is not None:
                tp_target_player = author
            
            if pid == self.server_chat_id and content.text.startswith(("Teleport", "Command tp")):
                tp_target_player = next(filter(lambda p: p.name == content.target, self.players.values()), None)
                discord_message_text = f"{author_name} @ {discord.utils.escape_markdown(content.target)}: `{discord.utils.escape_markdown(content.text)}`"
                em = None

            if pid == self.server_chat_id and content.text.startswith(("Moved to", "Already in")):
                discord_message_text = f"{author_name} @ {discord.utils.escape_markdown(content.target)}: `{discord.utils.escape_markdown(content.text)}`"
                em = None

            if tp_target_player and tp_target_player.channel is not None:
                if (chan := self.get_channel_by_id(tp_target_player.channel)) is not None and chan.name not in (None, "main"):
                    target_channel_name = chan.name
            print(f"{content.channel} ({content.whisper}) / {content.target} / {check_name} / {author_name} / '{content.text}'")
            if content.channel in ("main", None) and (check_name or (not content.whisper and pid != self.server_chat_id)) and not msg_lower.startswith(("/join !", "/channel !")):
                if not content.text.startswith("/") or msg_lower.startswith(("/join ", "/channel ")) or (msg_lower.startswith(("/e ", "/emote ")) and target_channel_name in (None, "", "main")):
                    for p in self.phrases:
                        content_stripped = re.sub(r'[^a-zA-Z ]', '', content.text if not check_name else content.target)
                        m = p.search(content.text if not check_name else content.target)
                        if m is None:
                            m = p.search(content_stripped if not check_name else content.target)
                        if m is not None:
                            naughty_word = m.group(0)
                            if discord_message_text is None:
                                discord_message_text = naughty_word
                            else:
                                discord_message_text = f"{discord_message_text} ({naughty_word})"
                            break
            purge: bool = False if not author else (content.target == author.name and (content.text.lower().startswith(("/gc ", "/globalchat ", "/r ", "/reply ")) or (not content.whisper and not content.text.startswith("/"))))
            ping: bool = (naughty_word is not None)
            was_delete: bool = (str(message.get('Color','')).lower() == "#ee2233")
            print(f"self.send_to_recipients {chat_id}, {pid}, {target_channel_name}, {discord_message_text}, {em}, {purge}, {ping}, {was_delete}")
            await self.send_to_recipients(chat_id, pid, target_channel_name, discord_message_text, em, purge, ping, was_delete)


    async def send_to_recipients(self, chat_id: int, pid: int, target_channel_name: str, discord_message_text: str, em: discord.Embed|None = None, purge: bool = False, ping: bool = False, was_delete: bool = False):
        if chat_id == -1:
            self.imaginary_chat_id -= 1
            chat_id = self.imaginary_chat_id
        for rec in self.recipients:
            rec_target_channel = rec.chat_channel
            drop_this = False
            if isinstance(target_channel_name, str) and target_channel_name not in ('', "main"):
                if rec.use_threads is False:
                    drop_this = True
                else:
                    target_channel_cnet: Channel = self.get_channel_by_name(target_channel_name)
                    if target_channel_cnet and rec.use_threads:
                        rec_target_channel = await rec.get_or_make_thread(target_channel_name, target_channel_cnet.ID)
                    if rec_target_channel is None:
                        await rec.status_message("WARN", f"Failed to create/get thread for channel {target_channel_name}.")
                        rec_target_channel = rec.chat_channel
            if rec_target_channel and not drop_this:
                print(f"rec.mq.insert_or_update {chat_id} {rec_target_channel}")
                await rec.mq.insert_or_update(chat_id, pid, content = discord_message_text, em = em, channel = rec_target_channel, purge=purge, ping = ping, was_delete=was_delete)

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

    @command_handler
    async def sess_join(self, data: str):
        try:
            data = json.loads(data)
        except json.decoder.JSONDecodeError:
            await self.status_message("WARN", f"Failed to parse cmd payload: {data}.")
            return
        print(f"cmd sess_join: {data}")
        pid = data['ID']
        if pid not in self.players:
            p = Player()
            self.players[pid] = p
        self.players[pid].dict_update(data)

        discord_message_text = f"**{self.players[pid].name}** ({self.players[pid].UID}) joined the server."
        naughty_word: str = None

        for p in self.phrases:
            m = p.search(self.players[pid].name)
            if m is not None:
                naughty_word = m.group(0)
                discord_message_text = f"{discord_message_text} ({naughty_word})"
                break
        should_ping = naughty_word is not None
        should_ping = self.check_name_ping_suppressed(self.players[pid].UID, should_ping)

        await self.send_to_recipients(-1, pid, '', discord_message_text, em=None, ping=should_ping)

    def check_name_ping_suppressed(self, uid: str, should_ping: bool):
        if uid in (None, ''):
            print(f'WARN: check_name_ping_suppressed with UID {uid}')
            return should_ping
        if uid in self.pinged_for_name:
            time_since_ping = datetime.datetime.now() - self.pinged_for_name[uid]
            if time_since_ping.total_seconds() > 3600*6:
                del self.pinged_for_name[uid]
            else:
                should_ping = False
        elif should_ping:
            self.pinged_for_name[uid] = datetime.datetime.now()
        return should_ping


    @command_handler
    async def sess_leave(self, data: str):
        try:
            data = json.loads(data)
        except json.decoder.JSONDecodeError:
            await self.status_message("WARN", f"Failed to parse cmd payload: {data}.")
            return
        print(f"cmd sess_leave: {data}")
        pid = data['ID']
        if pid in self.players:
            await self.send_to_recipients(-1, pid, '', f"**{self.players[pid].name}** ({self.players[pid].UID}) left the server.")
            del self.players[pid]

    @command_handler
    async def filter(self, data: str):
        try:
            data = json.loads(data)
        except json.decoder.JSONDecodeError:
            await self.status_message("WARN", f"Failed to parse cmd payload: {data}.")
            return
        print(f"cmd filter: {data}")
        pid = data['PlayerID']
        h = data['Handling']
        p: Player = None
        if pid in self.players:
            p = self.players[pid]
        uid: str = p.UID if p is not None else '?'
        if h == "Kick":
            should_ping = True
            if data['Cause'] == 'UserName':
                if uid not in (None, '', '?'):
                    should_ping = self.check_name_ping_suppressed(uid, should_ping)
                else:
                    print(f'WARN: Unknown UID {uid} during filter cmd')
            await self.send_to_recipients(-1, pid, '', f"**{data['Name']}** ({data['PlayerID']} / {uid}) auto-kicked for '{data['Cause']}': [{data['Tag']}] ||{data['Text']}||", ping=should_ping)

    @command_handler
    async def chan_move(self, data: str):
        try:
            data = json.loads(data)
        except json.decoder.JSONDecodeError:
            await self.status_message("WARN", f"Failed to parse chan_move cmd payload: {data}.")
            return
        print(f"cmd chan_move: {data}")
        pid = data['SessionID']
        fromid = data['fromID']
        toid = data['toID']
        if fromid in self.channels:
            if pid in self.channels[fromid].players:
                self.channels[fromid].players.remove(pid)
        if toid in self.channels:
            if pid not in self.channels[toid].players:
                self.channels[toid].players.append(pid)
        if pid in self.players:
            self.players[pid].channel = toid

    @command_handler
    async def chan_create(self, data: str):
        try:
            data = json.loads(data)
        except json.decoder.JSONDecodeError:
            await self.status_message("WARN", f"Failed to parse chan_create cmd payload: {data}.")
            return
        print(f"cmd chan_create: {data}")
        if len(self.channels)+1 != data['Count']:
            await self.get_channels()
        else:
            cid = data['Channel']['ID']
            if cid not in self.channels:
                c = Channel()
                self.channels[cid] = c
            self.channels[cid].dict_update(data['Channel'])
            for pid in self.channels[cid].players:
                if pid in self.players:
                    self.players[pid].channel = cid

    @command_handler
    async def chan_remove(self, data: str):
        try:
            data = json.loads(data)
        except json.decoder.JSONDecodeError:
            await self.status_message("WARN", f"Failed to parse chan_remove cmd payload: {data}.")
            return
        print(f"cmd chan_remove: {data}")
        if len(self.channels)+1 != data['Count']:
            await self.get_channels()
        else:
            cid = data['Channel']['ID']
            if cid in self.channels:
                del self.channels[cid]

    async def update_bot_status(self):
        if self.status is None or not isinstance(self.status, dict):
            print(f"Unknown status: {self.status}.")
            return

        print(f"Upd Status: {self.status}")
        self.activity.name=f"TCP/UDP: {self.status.get('TCPConnections', '?')}/{self.status.get('UDPConnections', '?')} ({len(asyncio.all_tasks(self.client.loop))})"
        self.activity.timestamps={"start": self.status.get('StartupTime', 0)/1000}
        await self.client.change_presence(activity=self.activity)

        tcp = self.status.get('TCPConnections', 0) or 0
        udp = self.status.get('UDPConnections', 0) or 0
        should_ping = (tcp > 2) and (udp < max(tcp * .25, 2))

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
        if endpoint == '/auth' and not self.backoff_auth.retry():
            return None

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
        try:
            print(f"Auth'ing with {self.cookies}")
            response = requests_method(self.api_base + endpoint, data=requests_data, cookies=self.cookies, timeout=8)
        except requests.exceptions.ReadTimeout:
            await self.status_message("WARN", f"Failed api call {requests_method} to {endpoint} (Read timeout)")
            return None
        if response.status_code != 200:
            await self.status_message("WARN", f">> {response.text}")
            return None

        if raw:
            return response.text

        try:
            return response.json()
        except json.decoder.JSONDecodeError:
            await self.status_message("WARN", f"Error decoding {endpoint} response: {response.text}")
            return None

    async def clear_players(self):
        self.players = {}
        self.players[self.server_chat_id] = Player(self.server_chat_id, "** SERVER **")

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

        old_startup = self.status.get('StartupTime', 0) if self.status else 0

        self.last_status_update = time.time()
        self.status = await self.api_fetch("/status")

        if self.status and 'StartupTime' in self.status and self.status['StartupTime'] != old_startup:
            await self.reauth()

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

                if self.auto_restart_metrics.update():
                    await self.status_message("INFO", dedent(f"""
                                                Auto-restarter in effect after {self.auto_restart_metrics.cooldown/3600.0} hours
                                                at <t:{int(self.auto_restart_metrics.exceeded_cooldown_at)}:R>
                                                and threshold adjusted to {self.auto_restart_metrics.player_limit_current} players.
                                                """), False)

                if self.auto_restart_metrics.should_restart(len(self.players)):
                    await self.status_message("INFO", dedent(f"""
                                                Auto-restarter metric has exceeded {self.auto_restart_metrics.cooldown/3600.0} hours
                                                at <t:{int(self.auto_restart_metrics.exceeded_cooldown_at)}:R>
                                                and dropped below {self.auto_restart_metrics.player_limit_low} + {self.auto_restart_metrics.player_limit_current - self.auto_restart_metrics.player_limit_low} players.
                                                """), False)
                    if self.sched_restart:
                        await self.status_message("WARN", dedent("Skipping auto-restart, because of scheduled restart!"))
                    else:
                        await self.schedule_restart(10)
                    self.auto_restart_metrics.reset()

                await self.schedule_res_update()

                await asyncio.sleep(1)
            except asyncio.CancelledError:
                print("Process task received CancelledError. Exiting.")
                return

    async def reauth(self):
        auth = None
        while auth == None:
            self.backoff_auth.update()
            auth = await self.api_fetch("/auth", requests.get)
            await asyncio.sleep(5)

        if isinstance(auth, dict) and 'Key' in auth:
            self.cookies['celestenet-session'] = auth['Key']
            await self.status_message(f"Auth: {auth['Info']}")
            self.ws_needs_reauth = True
            self.backoff_auth.reset()
        else:
            self.cookies.pop('celestenet-session', None)
            await self.status_message(f"Key not in reauth: {auth}")
        return auth

    async def cnet_chat_broadcast(self, msg: str, color: str = None):
        if color:
            return await self.invoke_wscmd("chatx", {"Color": color, "Text": msg})
        else:
            return await self.invoke_wscmd("chat", msg)

    async def do_restart(self, force: bool = False):
        payload=os.getenv("CNET_RESTART_REQ")
        try:
            payload=json.loads(payload)
        except json.JSONDecodeError:
            pass

        if not force and not (await self.failsafe_check()):
            return None
        ret = requests.post(os.getenv("CNET_RESTART_URI"), data=payload, timeout=180)
        await self.clear_players()
        # self.auto_restart_metrics.is_active = False
        return ret

    async def schedule_restart(self, minutes: int = 1):
        if self.sched_restart:
            return f"Invalid: Restart already scheduled for {self.sched_res_init_at + self.sched_res_delay}."
        
        self.sched_restart = True
        self.sched_res_delay = minutes
        self.sched_res_init_at = time.time()
        self.sched_last_notif_at = 0
        self.sched_next_notif_at = time.time()
    
    async def failsafe_check(self):
        if self.restarter_failsafe_tripped:
            return False

        seconds_since_last_restart = time.time() - self.restarter_last_restart
        if seconds_since_last_restart > 120:
            return True
                
        self.restarter_failsafe_counter += 1
        if self.restarter_failsafe_counter > 3:
            self.restarter_failsafe_tripped = True

        await self.status_message("WARN", f"Restarter failsafe check failed because last restart was at {self.restarter_last_restart}, {seconds_since_last_restart} seconds ago! ({self.restarter_failsafe_tripped=})")

        return False

    def reset_failsafe(self):
        self.restarter_failsafe_counter = 0
        self.restarter_failsafe_tripped = False

    async def schedule_res_update(self):
        if not self.sched_restart:
            await self.scheduled_restart_cancel()
            return
        
        should_notif = False

        if time.time() >= self.sched_next_notif_at and self.sched_next_notif_at > 0:
            if self.sched_last_notif_at < self.sched_next_notif_at:
                self.sched_last_notif_at = self.sched_next_notif_at
                should_notif = True
        
        await self.calc_schedule_next_notif()

        if self.seconds_until_sched_res <= 0:
            await self.status_message("!restart", f"Scheduled server restart executing... ({self.seconds_until_sched_res} / {self.seconds_since_sched_res})")
            await self.do_restart()
            await self.scheduled_restart_cancel()
            return
        
        if should_notif:
            await self.cnet_chat_broadcast(f"Scheduled server restart in {int(self.seconds_until_sched_res)} seconds...", "FCFF59")
            await self.status_message("Would !say", f"Scheduled server restart in {self.seconds_until_sched_res} seconds... (sched since {self.seconds_since_sched_res})")

    async def scheduled_restart_cancel(self):
        self.sched_restart = False
        self.sched_last_notif_at = 0
        self.sched_next_notif_at = 0
        return

    @property
    def seconds_since_sched_res(self):
        return time.time() - self.sched_res_init_at
    
    @property
    def seconds_until_sched_res(self):
        return self.sched_res_delay * 60 - self.seconds_since_sched_res

    async def calc_schedule_next_notif(self):
        if not self.sched_restart or self.seconds_until_sched_res <= 0:
            self.sched_last_notif_at = 0
            self.sched_next_notif_at = 0
            return
        
        if time.time() >= self.sched_next_notif_at and self.sched_next_notif_at > 0:
            if self.seconds_until_sched_res < 3 * 60:
                self.sched_next_notif_at = time.time() + 60
            elif self.seconds_until_sched_res > 8 * 60:
                self.sched_next_notif_at = time.time() + 5 * 60

    def status_of_restarters(self):
        return f"""{self.sched_restart=}
{self.sched_res_init_at=}
{self.sched_last_notif_at=}
{self.sched_next_notif_at=}
{self.seconds_until_sched_res=}
{self.seconds_since_sched_res=}
{self.restarter_failsafe_tripped=}

{self.auto_restart_metrics.is_active=}
{self.auto_restart_metrics.initial_start_delaying=}
{self.auto_restart_metrics.initial_delay_to_hour=}
{self.auto_restart_metrics.initial_start_time=}
{self.auto_restart_metrics.next_possible_restart=}
{self.auto_restart_metrics.cooldown=}
{self.auto_restart_metrics.exceeded_cooldown=}
{self.auto_restart_metrics.exceeded_cooldown_at=}
{self.auto_restart_metrics.force_at_limit=}
{self.auto_restart_metrics.player_limit_low=}
{self.auto_restart_metrics.player_limit_high=}
{self.auto_restart_metrics.player_limit_current=}
{self.auto_restart_metrics.limit_step_up_window=}
{self.auto_restart_metrics.player_limit_step_up=}
{self.auto_restart_metrics.next_step_up_at=}
        """

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

        await self.reauth()

        await self.get_status()
        await self.get_players()
        await self.get_channels()

        async for ws in websockets.connect(self.uri, origin=self.origin):
            self.ws = ws
            if 'celestenet-session' in self.cookies:
                await ws.send("cmd")
                await ws.send("reauth")
                await ws.send(json.dumps(self.cookies['celestenet-session']))
                self.ws_needs_reauth = False
                await self.get_status()
                await self.get_players()
                await self.get_channels()

            while True:
                try:
                    awaits_result: int|None = None
                    if len(self.ws_queue) > 0:
                        for k, v in self.ws_queue.items():
                            if v.result is None:
                                awaits_result = k

                    if self.ws_needs_reauth and 'celestenet-session' in self.cookies:
                        await ws.send("cmd")
                        await ws.send("reauth")
                        await ws.send(json.dumps(self.cookies['celestenet-session']))
                        self.ws_needs_reauth = False

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
                            if awaits_result is None:
                                print(f"Got ws 'data' but wasn't awaiting it: {ws_data}")
                            else:
                                self.ws_queue[awaits_result].result = ws_data
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
                    break
            for k, v in self.ws_queue.items():
                self.ws_queue[k].result = "Websocket died."
            self.ws = None
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
        def __init__(self, content: str = "", em: discord.Embed = None, channel: discord.TextChannel = None, ping: discord.Role = None):
            self.embed = em
            self.ping = ping
            self.content = content if ping is None else f"{content} {self.ping.mention}"
            self.msg = None
            self.channel = channel
            self.sent_in = None
            self.need_update = False

        def update(self, content: str = "", em: discord.Embed = None, channel: discord.TextChannel = None, ping: discord.Role = None):
            self.embed = em
            self.ping = ping
            self.content = content if ping is None else f"{content} {self.ping.mention}"
            self.channel = channel
            self.need_update = True

        def sent(self):
            return self.msg is not None and self.sent_in is not None

        async def send(self):
            if self.channel is None:
                print(f"=== Warning ===\n Tried to send {self.content} ({self.embed}) but channel is {self.channel}")
                return
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
        def __init__(self, chat_msg, discord_msg):
            self.recv_time = MessageQueue.timestamp()
            self.chat: MessageQueue.ChatMsg = chat_msg
            self.discord: MessageQueue.DiscordMsg = discord_msg
            self.purged = False

    @staticmethod
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
                if drop_msg.discord.sent_in is None and not drop_msg.purged:
                    print(f"popped message before it was sent: {drop_msg.discord.content}")

    def get_by_chat_id(self, chat_id: int):
        return next(filter(lambda m: m.chat.chat_id == chat_id, self.queue), None)

    def get_by_msg_id(self, msg_id: int):
        return next(filter(lambda m: isinstance(m.discord.msg, discord.Message) and m.discord.msg.id == msg_id, self.queue), None)

    async def insert_or_update(self, chat_id: int, user_id: int, content: str = None, em: discord.Embed = None, channel: discord.TextChannel = None, purge: bool = False, ping: bool = False, was_delete: bool = False):
        async with self.lock:
            found_msg = self.get_by_chat_id(chat_id)

            print(f"Checking for {chat_id}: {found_msg}")

            if isinstance(found_msg, MessageQueue.Message):
                if found_msg.discord.embed is not None:
                    print(f"Embed: {found_msg.discord.embed} / desc: {found_msg.discord.embed.description}")

                if purge or found_msg.purged:
                    print(f"Purging from MQ: {found_msg.chat.chat_id} {user_id}")
                    found_msg.purged = True
                    return
                found_msg.chat.user = user_id

                if was_delete:
                    if found_msg.discord.content is None:
                        found_msg.discord.content = "(deleted)"
                    else:
                        found_msg.discord.content = found_msg.discord.content + " (deleted)"
                    found_msg.discord.update(found_msg.discord.content, found_msg.discord.embed, channel, self.status_role if ping else None)
                    return

                found_msg.discord.update(content, em, channel, self.status_role if ping else None)
                return

            new_msg = MessageQueue.Message(
                MessageQueue.ChatMsg(chat_id, user_id),
                MessageQueue.DiscordMsg(content, em, channel, self.status_role if ping else None)
            )
            if purge:
                new_msg.purged = True

            #print(f"[{MessageQueue.timestamp()}] Inserting into MQ: {new_msg.recv_time} {new_msg.chat.chat_id} {new_msg.discord.channel}")
            self.queue.insert(0, new_msg)

    async def process(self):
        async with self.lock:
            for m in reversed(self.queue):
                if not m.purged:
                    if m.discord.need_update or (not m.discord.sent() and MessageQueue.timestamp() - m.recv_time > self.delay_before_send):
                        await m.discord.send()
