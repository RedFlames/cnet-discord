import os
import asyncio
import traceback
import json
import time

from enum import Enum, auto

import websockets
import requests
import dotenv

class State(Enum):
    Invalid = -1

    WaitForType = auto()

    WaitForCMDID = auto()
    WaitForCMDPayload = auto()

    WaitForData = auto()

dotenv.load_dotenv()

ws_commands = {}

class CelesteNetChat:

    cookies = {}

    def __init__(self):
        self.cookies = os.getenv("CNET_COOKIE")
        if isinstance(self.cookies, str):
            self.cookies = json.loads(self.cookies)

        self.command_state = State.WaitForType
        self.command_current = None

        self.ws_uri: str = 'wss://celestenet.0x0a.de/api/ws'
        self.origin: str = 'https://celestenet.0x0a.de'
        self.api_base: str = self.origin + '/api'

        self.players: dict[int, str] = {}
        self.channels: dict[int, str] = {}
        self.player_in_channel: dict[int, int] = {}

    def status_message(self, prefix="INFO", message=""):
        print(f"[{prefix}] {message}")

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
            self.status_message("WARN", f"Failed api call {requests_method} to {endpoint} with status {response.status_code}")
            self.status_message("WARN", f">> {response.text}")
            return None

        if raw:
            return response.text

        try:
            return response.json()
        except json.decoder.JSONDecodeError:
            self.status_message("WARN", f"Error decoding {endpoint} response: {response.text}")
            return None


    def get_players(self):
        """Wrapper logic around /api/players
        """
        players = self.api_fetch("/players")
        if isinstance(players, list):
            for pdict in players:
                pid = pdict['ID']
                self.players[pid] = pdict['FullName']
                if pid not in self.player_in_channel:
                    self.player_in_channel[pid] = 0

    def get_status(self):
        """Wrapper logic around /api/status
        """
        self.status_message(str(self.api_fetch("/status")))

    def get_channels(self):
        """Wrapper logic around /api/channels
        """
        channels = self.api_fetch("/channels")
        if isinstance(channels, list):
            for cdict in channels:
                cid = cdict['ID']
                self.channels[cid] = cdict['Name']
                for pid in cdict['Players']:
                    if pid in self.players:
                        self.player_in_channel[pid] = cid

    @staticmethod
    def command_handler(cmd_fn):
        ws_commands[cmd_fn.__name__] = cmd_fn

    @command_handler
    def chat(self, data: str):
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            self.status_message("ERROR",  f"Failed to parse chat payload: {data}")
            return

        # idk do stuff here lol I'm getting tired of copying from celestenet.py
        print(data.replace('\n',' '))


    @command_handler
    def update(self, data: str):
        try:
            data = json.loads(data)
        except json.decoder.JSONDecodeError:
            self.status_message("WARN", f"Failed to parse update cmd payload: {data}.")
            return
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

    async def socket_relay(self):
        """ 
            - tries to auth with cookies and such
            - opens and re-opens websocket connection with an endless loop
            - attempts auth against WS too
            - handles the various WS commands
        """

        auth = self.api_fetch("/auth", requests.get)

        if isinstance(auth, dict) and 'Key' in auth:
            self.cookies['celestenet-session'] = auth['Key']
            self.status_message(f"Auth: {auth['Info']}")
        else:
            self.cookies.pop('celestenet-session', None)
            self.status_message(f"Key not in reauth: {auth}")

        self.get_status()
        self.get_players()
        self.get_channels()

        async for ws in websockets.connect(self.ws_uri, origin=self.origin):
            if 'celestenet-session' in self.cookies:
                await ws.send("cmd")
                await ws.send("reauth")
                await ws.send(json.dumps(self.cookies['celestenet-session']))

            while True:
                try:
                    ws_data = await ws.recv()

                    match self.command_state:
                        case State.WaitForType:
                            match ws_data:
                                case "cmd":
                                    self.command_state = State.WaitForCMDID
                                case "data":
                                    self.command_state = State.WaitForData
                                case _:
                                    print(f"Unknown ws 'type': {ws_data}")
                                    break

                        case State.WaitForCMDID:
                            ws_cmd = None
                            if ws_data in ws_commands:
                                ws_cmd = ws_commands[ws_data]

                            if ws_cmd is None:
                                print(f"Unknown ws command: {ws_data}")
                                break
                            else:
                                self.command_current = ws_cmd
                                self.command_state = State.WaitForCMDPayload

                        case State.WaitForCMDPayload:
                            if self.command_current:
                                self.command_current(self, ws_data)
                                self.command_state = State.WaitForType
                                self.command_current = None
                            else:
                                print(f"Got payload but no current command: {ws_data}")
                                self.command_current = None
                                break

                        case State.WaitForData:
                            print(f"Got ws 'data' which isn't properly implemented: {ws_data}")
                            self.command_state = State.WaitForType
                            self.command_current = None

                        case _:
                            print(f"Unknown ws state: {command_state}")
                            self.command_state = State.WaitForType
                            self.command_current = None

                except websockets.ConnectionClosed:
                    self.status_message("WARN", "websocket died.")
                    break

celery = CelesteNetChat()
asyncio.get_event_loop().run_until_complete(celery.socket_relay())
