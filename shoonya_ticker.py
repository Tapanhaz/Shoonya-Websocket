
# -*- coding: utf-8 -*-
"""
    :description: Shoonya Ticker using picows.
    :author: Tapan Hazarika
    :created: On Thursday Aug 29, 2024 21:58:18 GMT+05:30
"""
__author__ = "Tapan Hazarika"

import ssl
import json
import uvloop
import asyncio
import logging
from enum import Enum
from itertools import islice
from functools import partial
from typing import Any, Union, List, Literal, Generator
from picows import ws_connect, WSFrame, WSTransport, WSListener, WSMsgType

logger = logging.getLogger(__name__)
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

class FeedType(Enum):
    TOUCHLINE = 1
    SNAPQUOTE = 2

class ShoonyaTicker:
    token_limit = 30
    ping_interval = 3
    def __init__(
            self, 
            ws_endpoint: str, 
            userid: str, 
            token: str
            ) -> None:
        self._ws_endpoint = ws_endpoint 
        self._userid = userid 
        self._token = token 
        self._stop_event = asyncio.Event()
        self.transport: WSTransport= None
        self.snapquote_list = []
        self.touchline_list = []
        self.__subscribe_callback = None
        self.__order_update_callback = None
        self.__on_error = None

    @staticmethod
    def create_client_ssl_context()-> ssl.SSLContext:
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ssl_context.load_default_certs(ssl.Purpose.SERVER_AUTH)
        ssl_context.check_hostname = False
        ssl_context.hostname_checks_common_name = False
        ssl_context.verify_mode = ssl.CERT_REQUIRED
        return ssl_context
    
    @staticmethod
    def _encode(msg: str)-> bytes:
        return json.dumps(msg).encode("utf_8")  

    @staticmethod
    def list_chunks(
            lst: List[str], 
            chunk_size: int= 30
            )-> Generator[List[str], None, None]:
        it = iter(lst)
        while True:
            chunk = list(islice(it, chunk_size))
            if not chunk:
                break
            yield chunk  

    def _ws_send(
            self, 
            msg: dict, 
            type: WSMsgType = WSMsgType.BINARY
            )-> None:
        payload = self._encode(msg)
        self.transport.send(type, payload)

    async def _ws_run_forever(self)-> None:
        while not self._stop_event.is_set():
            try:
                logger.debug("sending ping")
                self._ws_send({"t": "h"})
                await asyncio.sleep(self.ping_interval)
            except Exception as e:
                logger.warning(f"websocket run forever ended in exception, {e}")
            await asyncio.sleep(.1)

    def on_data_callback(
            self, 
            msg: str
            )-> None:
        try:
            msg = msg.decode("utf-8")
            msg = json.loads(msg)
        except Exception as e:
            logger.error(f"WS message error : {e}")
            return
        if self.__subscribe_callback:
            if msg["t"] in ("tk", "tf", "dk", "df"):
                self.__subscribe_callback(msg)
                return
        if self.__order_update_callback:
            if msg["t"] == "om":
                self.__order_update_callback(msg)
                return
        if self.__on_error:
            if msg["t"] == "ck" and msg["s"] != "OK":
                self.__on_error(msg)
                return
        if msg["t"] == "ck" and msg["s"] == "OK":  
            if self.snapquote_list:
                if len(self.snapquote_list) < self.token_limit:
                    self.subscribe(
                        instrument=self.snapquote_list, 
                        feed_type=FeedType.SNAPQUOTE
                        )
                else:
                    map(
                        partial(
                            self.subscribe,
                            feed_type = FeedType.SNAPQUOTE
                        ),
                        self.list_chunks(self.snapquote_list)
                    )
            if self.touchline_list:
                if len(self.touchline_list) < self.token_limit:
                    self.subscribe(
                        instrument=self.touchline_list, 
                        feed_type=FeedType.TOUCHLINE
                        )
                else:
                    map(
                        partial(
                            self.subscribe,
                            feed_type = FeedType.TOUCHLINE
                        ),
                        self.list_chunks(self.touchline_list)
                    )

            loop = asyncio.get_running_loop()
            loop.create_task(self._ws_run_forever())

    def subscribe(
            self, 
            instrument: Union[str, list], 
            feed_type: Literal[FeedType.SNAPQUOTE, FeedType.TOUCHLINE, "t", "d"]=FeedType.SNAPQUOTE
            )-> None:
        values = {}
        if feed_type == FeedType.TOUCHLINE or feed_type == "t":
            values["t"] = "t"
            if isinstance(instrument, list):
                values["k"] = "#".join(instrument)
                self.touchline_list.extend(instrument)
            else:
                values["k"] = instrument
                self.touchline_list.append(instrument)
            self._ws_send(values)
        elif feed_type == FeedType.SNAPQUOTE or feed_type == "d":
            values["t"] = "d"
            if isinstance(instrument, list):
                values["k"] = "#".join(instrument)
                self.snapquote_list.extend(instrument)
            else:
                values["k"] = instrument
                self.snapquote_list.append(instrument)
            self._ws_send(values)
    
    def unsubscribe(
            self, 
            instrument: Union[str, list], 
            feed_type: Literal[FeedType.SNAPQUOTE, FeedType.TOUCHLINE, "t", "d"]=FeedType.SNAPQUOTE
            )-> None:
        values = {}

        if feed_type == FeedType.TOUCHLINE or feed_type == "t":
            values["t"] = "u"
        elif feed_type == FeedType.SNAPQUOTE or feed_type == "d":
            values["t"] = "ud"

        if isinstance(instrument, list):
            values["k"] = "#".join(instrument)
        else:
            values["k"] = instrument
        self.__ws_send(values)

    async def start_ticker(self)-> None:
        ssl_context = self.create_client_ssl_context()
        ws_endpoint = self._ws_endpoint + self._token

        client = ShoonyaClient()
        client.parent = self 
        _, client = await ws_connect(
                            lambda: client, 
                            ws_endpoint, 
                            ssl_context=ssl_context
                            )
        await client.transport.wait_disconnected()
    
    def start_websocket(
                self,
                subscribe_callback: Any= None,
                order_update_callback: Any= None,
                error_callback: Any= None                        
            )-> None:
        self.__subscribe_callback = subscribe_callback
        self.__order_update_callback = order_update_callback
        self.__on_error = error_callback
        loop = asyncio.get_event_loop()
        loop.create_task(self.start_ticker())
    
    def close_websocket(self)-> None:
        self.transport.send_close()


class ShoonyaClient(WSListener):
    def __init__(self) -> None:
        super().__init__()
        self._full_msg = bytearray()

    def on_ws_connected(
            self, 
            transport: WSTransport
            )-> None:
        self.transport = transport
        self.parent.transport = transport 
        self._websocket_connected = True
        values = {"t": "c"}
        values["uid"] = self.parent._userid        
        values["actid"] = self.parent._userid
        values["susertoken"] = self.parent._token
        values["source"] = 'API'                
        self.parent._ws_send(values)

    def on_ws_frame(
            self, 
            transport: WSTransport, 
            frame: WSFrame
            )-> None:
        if frame.fin:
            if self._full_msg:
                self._full_msg += frame.get_payload_as_memoryview()
                #msg = self._full_msg.decode("utf-8")
                msg = self._full_msg
                self._full_msg.clear()
            else:
                #msg = frame.get_payload_as_utf8_text()  # create error on close
                msg = frame.get_payload_as_bytes()
            self.parent.on_data_callback(msg)
        else:
            self._full_msg += frame.get_payload_as_memoryview()
    
    def on_ws_disconnected(
            self,
            transport: WSTransport        
            )-> None:
        self.parent._stop_event.set()
        logger.info("Websocket disconnected.")
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(asyncio.create_task, self.shutdown(loop))

    @staticmethod
    async def shutdown(loop):
        tasks = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task()
            ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        loop.stop()    

    
