
# -*- coding: utf-8 -*-
"""
    :description: Shoonya Ticker using picows.
    :author: Tapan Hazarika
    :created: On Thursday Aug 29, 2024 21:58:18 GMT+05:30
"""
__author__ = "Tapan Hazarika"

import ssl
import socket
import orjson
import signal
import asyncio
import logging
import platform
from enum import Enum
from itertools import islice
from functools import partial, wraps
from typing import Any, Union, List, Dict, Literal, Generator, Optional
from picows import ws_connect, WSFrame, WSTransport, WSListener, WSMsgType, WSCloseCode, WSAutoPingStrategy

if platform.system() == "Windows":
    import winloop
    asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
else:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

logger = logging.getLogger(__name__)



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
            token: str,
            loop: Optional[asyncio.AbstractEventLoop]= None
            ) -> None:
        self._ws_endpoint = ws_endpoint 
        self._userid = userid 
        self._token = token 
        self._stop_event = asyncio.Event()
        self.IS_CONNECTED = asyncio.Event()
        #self._pong_event = asyncio.Event()
        self.transport: WSTransport= None
        self.snapquote_list = []
        self.touchline_list = []
        self.__on_error = None
        self.__on_open = None
        self._on_close= None
        self._disconnect_socket = False

        #self.__ping_msg = self._encode({"t": "h"})
        self.__disconnect_message = ShoonyaTicker._encode("Connection closed by the user.")

        self._loop = loop if loop else asyncio.get_event_loop()
        self.add_signal_handler()

        self.__callback_map = {
            "ck": partial(self.__handle_connection_message),
            "udk": ShoonyaTicker.__unsubscribe_callback,
            "uk": ShoonyaTicker.__unsubscribe_callback,
            "am": ShoonyaTicker.__alert_message_callback
            }
    
    @staticmethod
    def run_in_thread():
        def decorator(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                return await asyncio.to_thread(lambda: func(*args, **kwargs))
            return wrapper
        return decorator

    @staticmethod
    def create_client_ssl_context()-> ssl.SSLContext:
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ssl_context.load_default_certs(ssl.Purpose.SERVER_AUTH)
        ssl_context.check_hostname = False
        ssl_context.hostname_checks_common_name = False
        #ssl_context.verify_mode = ssl.CERT_REQUIRED
        ssl_context.verify_mode = ssl.CERT_NONE
        return ssl_context
    
    @staticmethod
    async def _dummy_callback(msg)-> None:
        #logger.info(msg)
        pass

    @staticmethod
    async def __unsubscribe_callback(msg):
        logger.info(msg)
    
    @staticmethod
    async def __alert_message_callback(msg):
        logger.info(msg)
    
    @staticmethod
    def _encode(msg: str)-> bytes:
        #return json.dumps(msg).encode("utf_8")  
        return orjson.dumps(msg)

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
    
    async def stop_signal_handler(self, *args, **kwargs)-> None:
        logger.info(f"WebSocket closure initiated by user interrupt.")
        self.close_websocket()  
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=2)
        except TimeoutError:
            self._initiate_shutdown() 

    def add_signal_handler(self):
        for signame in ('SIGINT', 'SIGTERM'):
            self._loop.add_signal_handler(
                                getattr(signal, signame),
                                lambda: asyncio.create_task(self.stop_signal_handler())
                                )

    def _ws_send(
            self, 
            msg: dict, 
            type: WSMsgType = WSMsgType.BINARY
            )-> None: 
        #logger.info(msg)       
        payload = ShoonyaTicker._encode(msg)
        self.transport.send(type, payload)

    #async def _ws_run_forever(self)-> None:
    #    self._pong_event.set()
    #    while not self._stop_event.is_set():
    #        try:
    #            await asyncio.wait_for(self._pong_event.wait(), timeout=20)
    #            await asyncio.sleep(3)
    #            logger.debug("sending ping")
    #            self.transport.send_ping(message= self.__ping_msg)
    #            self._pong_event.clear()
    #        except (TimeoutError, Exception) as e:
    #            logger.warning("Websocket run forever ended with an exception :: No PONG received from server")
    #            self.transport.underlying_transport.abort()
    #            break

    def on_data_callback(
            self, 
            msg: str
            )-> None:
        try:
            msg = orjson.loads(msg)
            msg_type = msg["t"] 
            self._loop.create_task(self.__callback_map[msg_type](msg))
        except (KeyError, Exception) as e:
            logger.error(f"WS message error : {e} :: {msg}")
            return
            
    async def __handle_connection_message(self, msg: dict) -> None:
        if msg["s"] != "OK" and self.__on_error:
            self._loop.create_task(self.__on_error(msg))
            return
        
        if msg["s"] == "OK":
            if self.snapquote_list:
                snapquote_temp = self.snapquote_list[:]
                self.snapquote_list.clear()
                await self.subscribe(
                    instrument=snapquote_temp, 
                    feed_type=FeedType.SNAPQUOTE
                    )

            if self.touchline_list:
                touchline_temp = self.touchline_list[:]
                self.touchline_list.clear()
                await self.subscribe(
                    instrument=touchline_temp, 
                    feed_type=FeedType.TOUCHLINE
                    )
            #self._loop.create_task(self._ws_run_forever())   
            if self.__on_open:
                self._loop.create_task(self.__on_open(msg)) 

    @staticmethod
    def __prepare_chunk_values(
            values: Dict[str, str],
            chunk: List[str]        
            )-> Dict[str, str]:
        values_copy = values.copy()
        values_copy["k"] = "#".join(chunk)
        return values_copy

    @run_in_thread()
    def subscribe(
            self, 
            instrument: Union[str, list], 
            feed_type: Literal[FeedType.SNAPQUOTE, FeedType.TOUCHLINE, "t", "d"]=FeedType.SNAPQUOTE
            )-> None:
        values = {}
        if feed_type == FeedType.TOUCHLINE or feed_type == "t":
            values["t"] = "t"
            if isinstance(instrument, list):
                if len(instrument) < self.token_limit:
                    values["k"] = "#".join(instrument)
                    self._ws_send(values)
                else:
                    values_chunks = list(
                                        map(
                                            partial(                                                
                                                ShoonyaTicker.__prepare_chunk_values,
                                                values                #values.copy()
                                                
                                            ),
                                            self.list_chunks(
                                                        instrument,
                                                        chunk_size= self.token_limit
                                                        )  
                                        )  
                                    )
                    list(map(self._ws_send, values_chunks))

                self.touchline_list.extend(instrument)
            else:
                values["k"] = instrument
                self.touchline_list.append(instrument)
                self._ws_send(values)
        elif feed_type == FeedType.SNAPQUOTE or feed_type == "d":
            values["t"] = "d"
            if isinstance(instrument, list):
                if len(instrument) < self.token_limit:
                    values["k"] = "#".join(instrument)
                    self._ws_send(values)
                else:
                    values_chunks = list(
                                        map(
                                            partial(
                                                ShoonyaTicker.__prepare_chunk_values,
                                                values              #values.copy()
                                            ),
                                            self.list_chunks(
                                                        instrument,
                                                        chunk_size= self.token_limit
                                                        )  
                                        )  
                                    )
                    list(map(self._ws_send, values_chunks))
                self.snapquote_list.extend(instrument)
            else:
                values["k"] = instrument
                self.snapquote_list.append(instrument)
                self._ws_send(values)
    
    @run_in_thread()
    def unsubscribe(
            self, 
            instrument: Union[str, list], 
            feed_type: Literal[FeedType.SNAPQUOTE, FeedType.TOUCHLINE, "t", "d"]=FeedType.SNAPQUOTE
            )-> None:
        values = {}

        if feed_type == FeedType.TOUCHLINE or feed_type == "t":
            values["t"] = "u"
            if isinstance(instrument, list):
                values["k"] = "#".join(instrument)
                self.touchline_list[:] = list(
                                                filter(
                                                    lambda i:i not in set(instrument),
                                                    self.touchline_list
                                                )    
                                            )
            else:
                values["k"] = instrument
                try:
                    self.touchline_list.pop(self.touchline_list.index(instrument))
                except ValueError:
                    pass
        elif feed_type == FeedType.SNAPQUOTE or feed_type == "d":
            values["t"] = "ud"
            if isinstance(instrument, list):
                values["k"] = "#".join(instrument)
                self.snapquote_list[:] = list(
                                                filter(
                                                    lambda i:i not in set(instrument),
                                                    self.snapquote_list
                                                )    
                                            )
            else:
                values["k"] = instrument
                try:
                    self.snapquote_list.pop(self.snapquote_list.index(instrument))
                except ValueError:
                    pass
        self._ws_send(values)

    async def start_ticker(self, reconnect: bool= False)-> None:
        ssl_context = ShoonyaTicker.create_client_ssl_context()
        ws_endpoint = self._ws_endpoint + self._token

        client = ShoonyaClient(
                        parent= self,
                        loop= self._loop
                        )
        #client.parent = self 
        try:
            transport, client = await ws_connect(
                                lambda: client, 
                                ws_endpoint, 
                                ssl_context=ssl_context,
                                enable_auto_ping= True,
                                auto_ping_idle_timeout= 3,
                                auto_ping_reply_timeout= 2,
                                auto_ping_strategy= WSAutoPingStrategy.PING_WHEN_IDLE
                                )
            await transport.wait_disconnected()
        except (socket.gaierror, OSError) as e:
            logger.error(f"Error occured on connect :: {e}")          
            if reconnect or str(e) == "websocket handshake timeout":
                await asyncio.sleep(1)
                return await self.start_ticker(reconnect=True)
            else:
                self._initiate_shutdown()
    
    def start_websocket(
                self,
                subscribe_callback: Any= _dummy_callback,
                order_update_callback: Any= _dummy_callback,
                error_callback: Any= None,
                open_callback: Any= None,
                close_callback: Any= None                                         
            )-> None:
        self.__subscribe_callback = subscribe_callback
        self.__order_update_callback = order_update_callback
        self.__on_error = error_callback
        self.__on_open = open_callback
        self._on_close = close_callback
        self.__callback_map = {
                    "df": self.__subscribe_callback,
                    "tf": self.__subscribe_callback,
                    "dk": self.__subscribe_callback,
                    "tk": self.__subscribe_callback,
                    "om": self.__order_update_callback,
                    **self.__callback_map
                    }
        
        self._loop.create_task(self.start_ticker())
    
    def close_websocket(self)-> None:
        self._disconnect_socket = True
        if self.transport:
            self.transport.send_close(
                            close_code= WSCloseCode.OK, 
                            close_message=self.__disconnect_message
                            )
    
    def _initiate_shutdown(self)-> None:
        self._stop_event.set()
        logger.info("Websocket disconnected.")
        self._loop.call_soon_threadsafe(asyncio.create_task, self.shutdown(self._loop))
        self.IS_CONNECTED.clear()

    @staticmethod
    async def shutdown(loop):
        tasks = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task()
            ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        loop.stop()  


class ShoonyaClient(WSListener):
    def __init__(
                self,
                parent: ShoonyaTicker,
                loop: asyncio.AbstractEventLoop
                ) -> None:
        super().__init__()
        self.__parent = parent
        self.__loop = loop
        #self._full_msg = bytearray()
        self.__ping_msg = ShoonyaTicker._encode({"t": "h"})

    def on_ws_connected(
            self, 
            transport: WSTransport
            )-> None:
        self.transport = transport
        self.__parent.transport = transport 
        values = {"t": "c"}
        values["uid"] = self.__parent._userid        
        values["actid"] = self.__parent._userid
        values["susertoken"] = self.__parent._token
        values["source"] = 'API'                
        self.__parent._ws_send(values)
        self.__parent.IS_CONNECTED.set()
    
    def send_user_specific_ping(self, transport):
        logger.debug("sending ping")
        transport.send_ping(message=self.__ping_msg)

    def on_ws_frame(
            self, 
            transport: WSTransport, 
            frame: WSFrame
            )-> None:  
        if frame.msg_type == WSMsgType.TEXT:
            msg = frame.get_payload_as_utf8_text()
            self.__parent.on_data_callback(msg)
            return
        if frame.msg_type == WSMsgType.PONG:
            #logger.info(frame)
            #self.parent._pong_event.set() 
            transport.notify_user_specific_pong_received()         
        elif frame.msg_type == WSMsgType.CLOSE:
            close_msg = frame.get_close_message()
            close_code = frame.get_close_code()
            if close_msg:
                close_msg = close_msg.decode()
            if close_code == 1008:
                self.__parent._disconnect_socket = True
                close_msg = "Invalid credentials."
            logger.info( f"Shoonya Ticker disconnected, code={close_code}, reason={close_msg}")
            transport.disconnect()
        else:
            logger.info(f"Shoonya is expected to send text messages, instead received {frame.msg_type}")

    def on_ws_disconnected(
            self,
            transport: WSTransport        
            )-> None:
        if self.__parent._on_close:
            self.__loop.create_task(self.__parent._on_close())
        if self.__parent._disconnect_socket:
            self.__parent._initiate_shutdown() 
        else: 
            logger.info("Trying to reconnect..")
            transport.disconnect()
            self.__loop.create_task(self.__parent.start_ticker(reconnect=True))
