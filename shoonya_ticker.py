"""
:description: Shoonya Ticker using picows. Now with AZ-aware
    per-IP failover (still fighting the 502s).
:author: Tapan Hazarika
:created: On Thursday Aug 29, 2024 21:58:18 GMT+05:30
:updated: On Thursday Aug 27, 2026 -- added failover reconnect
"""

__author__ = "Tapan Hazarika"

import asyncio
import errno
import logging
import platform
import random
import signal
import socket
import ssl
import time
from collections.abc import Generator
from enum import Enum
from functools import partial, wraps
from itertools import islice
from typing import Any, Literal
from urllib.parse import urlsplit

import orjson
from picows import (
    WSAutoPingStrategy,
    WSCloseCode,
    WSFrame,
    WSInvalidStatusError,
    WSListener,
    WSMsgType,
    WSParsedURL,
    WSTransport,
    ws_connect,
)

if platform.system() == "Windows":
    import winloop  # type: ignore

    asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
else:
    import uvloop  # type: ignore

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

logger = logging.getLogger(__name__)

__all__ = ["AccessType", "FeedType", "ShoonyaTicker"]


class FeedType(Enum):
    TOUCHLINE = 1
    SNAPQUOTE = 2


class AccessType(str, Enum):
    API = "API"
    WEB = "WEB"
    MOB = "MOB"


class ShoonyaTicker:
    token_limit = 30
    ping_interval = 3
    chunk_send_interval = 0.0

    resolve_timeout = 3
    handshake_timeout = 4
    quarantine_secs = 20

    min_backoff = 0.25
    max_backoff = 5.0
    backoff_multiplier = 2.0
    backoff_jitter = 0.3
    no_network_retry_interval = 0.5
    pinning_probe_interval = 300.0
    min_stable_before_probe = 30.0
    stall_alert_every = 5

    def __init__(
        self,
        ws_endpoint: str,
        userid: str,
        token: str,
        loop: asyncio.AbstractEventLoop | None = None,
        ssl_context: ssl.SSLContext | None = None,
        verify_ssl: bool = False,
        enable_ip_pinning: bool = False,
    ) -> None:
        self._ws_endpoint = ws_endpoint
        self._userid = userid
        self._token = token
        self._ssl_context = ssl_context
        self._verify_ssl = verify_ssl
        self._pinning_feature_enabled = enable_ip_pinning

        self._pinning_enabled = False
        self._ip_quarantine: dict[str, float] = {}
        self._last_used_ip: str | None = None
        self._consecutive_full_failures = 0
        self._abort_backoff_event = asyncio.Event()

        self._stop_event = asyncio.Event()
        self.IS_CONNECTED = asyncio.Event()

        self._last_pin_probe_at = 0.0
        self._last_pin_stable_duration = 0.0

        self._shutdown_initiated = False
        # self._pong_event = asyncio.Event()
        self.transport: WSTransport = None
        self.snapquote_list = []
        self.touchline_list = []
        self.__on_error = None
        self.__on_open = None
        self._on_close = None

        self.__on_stalled = None
        self._disconnect_socket = False
        self._access_type: AccessType = AccessType.API

        # self.__ping_msg = self._encode({"t": "h"})
        self.__disconnect_message = ShoonyaTicker._encode(
            "Connection closed by the user."
        )

        if not loop:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()

        self._loop = loop

        self.add_signal_handler()

        self.__callback_map = {
            "ak": partial(self.__handle_connection_message),
            "ck": partial(self.__handle_connection_message),
            "udk": ShoonyaTicker.__unsubscribe_callback,
            "uk": ShoonyaTicker.__unsubscribe_callback,
            "am": ShoonyaTicker.__alert_message_callback,
            "ms": ShoonyaTicker.__alert_message_callback,
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
    def create_client_ssl_context(verify: bool = False) -> ssl.SSLContext:
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        if verify:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_REQUIRED
        else:
            ssl_context.load_default_certs(ssl.Purpose.SERVER_AUTH)
            ssl_context.check_hostname = False
            ssl_context.hostname_checks_common_name = False
            ssl_context.verify_mode = ssl.CERT_NONE
        return ssl_context

    @staticmethod
    async def _resolve_candidates(
        hostname: str, port: int, loop: asyncio.AbstractEventLoop
    ) -> list[str]:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(
                hostname, port, family=socket.AF_INET, type=socket.SOCK_STREAM
            ),
            timeout=ShoonyaTicker.resolve_timeout,
        )
        seen, ips = set(), []
        for _, _, _, _, sockaddr in infos:
            ip = sockaddr[0]
            if ip not in seen:
                seen.add(ip)
                ips.append(ip)
        return ips

    def _candidate_order(self, candidates: list[str]) -> list[str]:
        now = time.monotonic()
        self._ip_quarantine = {
            ip: until for ip, until in self._ip_quarantine.items() if until > now
        }
        live = [ip for ip in candidates if ip not in self._ip_quarantine]
        quarantined = [ip for ip in candidates if ip in self._ip_quarantine]
        random.shuffle(live)
        random.shuffle(quarantined)
        return live + quarantined

    def _quarantine(self, ip: str) -> None:
        self._ip_quarantine[ip] = time.monotonic() + self.quarantine_secs
        logger.warning(f"Quarantining backend {ip} for {self.quarantine_secs}s")

    async def _sleep_no_network(self) -> None:
        logger.debug(
            f"No local network path -- retrying in {self.no_network_retry_interval:.2f}s"
        )
        try:
            await asyncio.wait_for(
                self._abort_backoff_event.wait(),
                timeout=self.no_network_retry_interval,
            )
        except (TimeoutError, asyncio.TimeoutError):
            pass
        else:
            logger.info("No-network retry wait aborted early -- close requested")

    async def _sleep_backoff(self) -> None:
        self._consecutive_full_failures += 1
        n = self._consecutive_full_failures
        delay = min(
            self.max_backoff, self.min_backoff * (self.backoff_multiplier ** (n - 1))
        )
        jitter = delay * self.backoff_jitter
        delay = max(0.0, delay + random.uniform(-jitter, jitter))
        logger.warning(
            f"Full reconnect cycle failed ({n} in a row) :: backing off {delay:.2f}s"
        )
        if self.__on_stalled and n % self.stall_alert_every == 0:
            self._loop.create_task(self.__on_stalled(self, n))
        try:
            await asyncio.wait_for(self._abort_backoff_event.wait(), timeout=delay)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        else:
            logger.info("Backoff sleep aborted early -- close requested mid-backoff")

    def _reset_backoff(self) -> None:
        self._consecutive_full_failures = 0

    def reset_failover_mode(self) -> None:
        self._pinning_enabled = False
        self._ip_quarantine.clear()

    async def _pinned_socket_factory(
        self, parsed_url: WSParsedURL
    ) -> socket.socket | None:
        try:
            candidates = await ShoonyaTicker._resolve_candidates(
                parsed_url.host, parsed_url.port, self._loop
            )
        except (socket.gaierror, asyncio.TimeoutError) as e:
            logger.error(f"DNS resolution failed :: {e}")
            raise

        now = time.monotonic()
        self._ip_quarantine = {
            ip: until for ip, until in self._ip_quarantine.items() if until > now
        }
        live = [ip for ip in candidates if ip not in self._ip_quarantine]
        quarantined = [ip for ip in candidates if ip in self._ip_quarantine]
        random.shuffle(live)
        random.shuffle(quarantined)
        live_count = len(live)

        last_exc: Exception | None = None
        for ip in live + quarantined:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            try:
                logger.debug(f"Trying with :: {ip}:{parsed_url.port}")
                await self._loop.sock_connect(sock, (ip, parsed_url.port))
            except OSError as e:
                sock.close()
                logger.warning(f"TCP connect to {ip} failed :: {e}")
                if ip in live:
                    if live_count > 1:
                        self._quarantine(ip)
                        live_count -= 1
                    else:
                        logger.debug(f"Not quarantining {ip} -- last live candidate")
                last_exc = e
                continue
            else:
                self._last_used_ip = ip
                self._ip_quarantine.pop(ip, None)
                return sock

        raise (
            last_exc
            if last_exc is not None
            else OSError(f"No candidates resolved for {parsed_url.host}")
        )

    @staticmethod
    async def _dummy_callback(msg) -> None:
        # logger.info(msg)
        pass

    @staticmethod
    async def __unsubscribe_callback(msg):
        logger.info(msg)

    @staticmethod
    async def __alert_message_callback(msg):
        logger.info(msg)

    @staticmethod
    def _encode(msg: str) -> bytes:
        # return json.dumps(msg).encode("utf_8")
        return orjson.dumps(msg)

    @staticmethod
    def list_chunks(
        lst: list[str], chunk_size: int = 30
    ) -> Generator[list[str], None, None]:
        it = iter(lst)
        while True:
            chunk = list(islice(it, chunk_size))
            if not chunk:
                break
            yield chunk

    async def stop_signal_handler(self, signum) -> None:
        if self._shutdown_initiated:
            return
        self._shutdown_initiated = True

        logger.info("WebSocket closure initiated by user interrupt.")
        self.close_websocket()
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=2)
        except TimeoutError:
            self._initiate_shutdown()

        signal.raise_signal(signum)

    def add_signal_handler(self):
        for signame in ("SIGINT", "SIGTERM"):
            signum = getattr(signal, signame)
            self._loop.add_signal_handler(
                signum,
                lambda s=signum: asyncio.create_task(self.stop_signal_handler(s)),
            )

    def _ws_send(self, msg: dict, type: WSMsgType = WSMsgType.BINARY) -> None:
        # logger.info(msg)
        payload = ShoonyaTicker._encode(msg)
        self._loop.call_soon_threadsafe(self.transport.send, type, payload)

    def on_data_callback(self, msg: str) -> None:
        try:
            msg = orjson.loads(msg)
            msg_type = msg["t"]
            self._loop.create_task(self.__callback_map[msg_type](msg))
        except (KeyError, Exception) as e:  # noqa: BLE001
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
                    instrument=snapquote_temp, feed_type=FeedType.SNAPQUOTE
                )

            if self.touchline_list:
                touchline_temp = self.touchline_list[:]
                self.touchline_list.clear()
                await self.subscribe(
                    instrument=touchline_temp, feed_type=FeedType.TOUCHLINE
                )
            # self._loop.create_task(self._ws_run_forever())
            if self.__on_open:
                self._loop.create_task(self.__on_open(msg))

    @staticmethod
    def __prepare_chunk_values(
        values: dict[str, str], chunk: list[str]
    ) -> dict[str, str]:
        values_copy = values.copy()
        values_copy["k"] = "#".join(chunk)
        return values_copy

    @run_in_thread()
    def subscribe(
        self,
        instrument: str | list,
        feed_type: Literal[
            FeedType.SNAPQUOTE, FeedType.TOUCHLINE, "t", "d"
        ] = FeedType.SNAPQUOTE,
    ) -> None:
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
                                values,  # values.copy()
                            ),
                            self.list_chunks(instrument, chunk_size=self.token_limit),
                        )
                    )
                    # list(map(self._ws_send, values_chunks))
                    for v in values_chunks:
                        self._ws_send(v)
                        time.sleep(self.chunk_send_interval)

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
                                values,  # values.copy()
                            ),
                            self.list_chunks(instrument, chunk_size=self.token_limit),
                        )
                    )
                    # list(map(self._ws_send, values_chunks))
                    for v in values_chunks:
                        self._ws_send(v)
                        time.sleep(self.chunk_send_interval)
                self.snapquote_list.extend(instrument)
            else:
                values["k"] = instrument
                self.snapquote_list.append(instrument)
                self._ws_send(values)

    @run_in_thread()
    def unsubscribe(
        self,
        instrument: str | list,
        feed_type: Literal[
            FeedType.SNAPQUOTE, FeedType.TOUCHLINE, "t", "d"
        ] = FeedType.SNAPQUOTE,
    ) -> None:
        values = {}

        if feed_type == FeedType.TOUCHLINE or feed_type == "t":
            values["t"] = "u"
            if isinstance(instrument, list):
                values["k"] = "#".join(instrument)
                self.touchline_list[:] = list(
                    filter(lambda i: i not in set(instrument), self.touchline_list)
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
                    filter(lambda i: i not in set(instrument), self.snapquote_list)
                )
            else:
                values["k"] = instrument
                try:
                    self.snapquote_list.pop(self.snapquote_list.index(instrument))
                except ValueError:
                    pass
        self._ws_send(values)

    def _pin_probe_due(self) -> bool:
        if self._last_pin_stable_duration < self.min_stable_before_probe:
            return False
        if time.monotonic() - self._last_pin_probe_at < self.pinning_probe_interval:
            return False
        self._last_pin_probe_at = time.monotonic()
        return True

    # reconnect is stale now; will remove in a later update

    async def start_ticker(self, reconnect: bool = False) -> None:
        if self._disconnect_socket:
            self._initiate_shutdown()
            return

        if self._pinning_enabled and self._pin_probe_due():
            logger.info("Pinned session was stable -- probing normal path for recovery")
            self._pinning_enabled = False

        ssl_context = self._ssl_context or ShoonyaTicker.create_client_ssl_context(
            verify=self._verify_ssl
        )
        full_url = self._ws_endpoint + self._token
        parts = urlsplit(full_url)
        __host = parts.hostname

        if not self._pinning_enabled:
            client = ShoonyaClient(parent=self, loop=self._loop)

            try:
                transport, client = await asyncio.wait_for(
                    ws_connect(
                        lambda client=client: client,
                        full_url,
                        ssl_context=ssl_context,
                        server_hostname=__host,
                        enable_auto_ping=True,
                        auto_ping_idle_timeout=3,
                        auto_ping_reply_timeout=2,
                        auto_ping_strategy=WSAutoPingStrategy.PING_WHEN_IDLE,
                        use_aiofastnet=True,
                    ),
                    timeout=self.handshake_timeout,
                )
            except socket.gaierror as e:
                logger.error(f"DNS unreachable -- no local network path :: {e}")
                await self._sleep_no_network()
                return await self.start_ticker(reconnect=True)
            except OSError as e:
                if e.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN):
                    logger.error(f"Network unreachable -- no local route :: {e}")
                    await self._sleep_no_network()
                elif self._pinning_feature_enabled:
                    logger.warning(
                        f"Connect to {__host} failed, switching to per-IP failover :: {e}"
                    )
                    self._pinning_enabled = True
                else:
                    logger.error(f"Connect to {__host} failed, backing off :: {e}")
                    await self._sleep_backoff()
                return await self.start_ticker(reconnect=True)
            except (asyncio.TimeoutError, WSInvalidStatusError) as e:
                if self._pinning_feature_enabled:
                    logger.warning(
                        f"Connect to {__host} failed, switching to per-IP failover :: {e}"
                    )
                    self._pinning_enabled = True
                else:
                    logger.error(f"Connect to {__host} failed, backing off :: {e}")
                    await self._sleep_backoff()
                return await self.start_ticker(reconnect=True)
            else:
                self._reset_backoff()
                logger.info(f"Connected to Shoonya via {__host}")
                await transport.wait_disconnected()
                return

        client = ShoonyaClient(parent=self, loop=self._loop)

        try:
            transport, client = await asyncio.wait_for(
                ws_connect(
                    lambda client=client: client,
                    full_url,
                    ssl_context=ssl_context,
                    server_hostname=__host,
                    socket_factory=self._pinned_socket_factory,
                    enable_auto_ping=True,
                    auto_ping_idle_timeout=3,
                    auto_ping_reply_timeout=2,
                    auto_ping_strategy=WSAutoPingStrategy.PING_WHEN_IDLE,
                    use_aiofastnet=True,
                ),
                timeout=self.handshake_timeout,
            )

        except socket.gaierror as e:
            logger.error(f"DNS unreachable -- no local network path :: {e}")
            await self._sleep_no_network()
            return await self.start_ticker(reconnect=True)
        except OSError as e:
            if e.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN):
                logger.error(f"Network unreachable -- no local route :: {e}")
                await self._sleep_no_network()
            else:
                logger.error(f"All failover candidates failed, backing off :: {e}")
                await self._sleep_backoff()
            return await self.start_ticker(reconnect=True)
        except (asyncio.TimeoutError, WSInvalidStatusError) as e:
            logger.error(f"All failover candidates failed, backing off :: {e}")
            await self._sleep_backoff()
            return await self.start_ticker(reconnect=True)

        else:
            self._reset_backoff()
            connected_at = time.monotonic()
            logger.info(f"Connected via {self._last_used_ip} (failover mode)")
            await transport.wait_disconnected()
            self._last_pin_stable_duration = time.monotonic() - connected_at
            return

    def start_websocket(
        self,
        subscribe_callback: Any = _dummy_callback,
        order_update_callback: Any = _dummy_callback,
        error_callback: Any = None,
        open_callback: Any = None,
        close_callback: Any = None,
        stalled_callback: Any = None,
        access_type: AccessType | Literal["API", "WEB", "MOB"] = "API",
    ) -> None:
        self.__subscribe_callback = subscribe_callback
        self.__order_update_callback = order_update_callback
        self.__on_error = error_callback
        self.__on_open = open_callback
        self._on_close = close_callback
        self.__on_stalled = stalled_callback
        self.__callback_map = {
            "df": self.__subscribe_callback,
            "tf": self.__subscribe_callback,
            "dk": self.__subscribe_callback,
            "tk": self.__subscribe_callback,
            "om": self.__order_update_callback,
            **self.__callback_map,
        }

        self._access_type = (
            access_type
            if isinstance(access_type, AccessType)
            else AccessType(access_type)
        )

        self._loop.create_task(self.start_ticker())

    def close_websocket(self) -> None:
        if self.transport is None or self._disconnect_socket:
            self._disconnect_socket = True
            self._abort_backoff_event.set()
            return

        self._disconnect_socket = True
        self._abort_backoff_event.set()
        self.transport.send_close(
            close_code=WSCloseCode.OK, close_message=self.__disconnect_message
        )

    def _initiate_shutdown(self) -> None:
        self._stop_event.set()
        logger.info("Websocket disconnected.")
        self._loop.call_soon_threadsafe(asyncio.create_task, self.shutdown(self._loop))
        self.IS_CONNECTED.clear()

    @staticmethod
    async def shutdown(loop):
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        loop.stop()


class ShoonyaClient(WSListener):
    def __init__(self, parent: ShoonyaTicker, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self.__parent = parent
        self.__loop = loop
        # self._full_msg = bytearray()
        self.__ping_msg = ShoonyaTicker._encode({"t": "h"})

    def on_ws_connected(self, transport: WSTransport) -> None:
        self.transport = transport
        self.__parent.transport = transport

        is_api: bool = self.__parent._access_type == AccessType.API

        if is_api:
            values = {"t": "a"}
        else:
            values = {"t": "c"}

        values["uid"] = self.__parent._userid
        values["actid"] = self.__parent._userid

        if is_api:
            values["accesstoken"] = self.__parent._token
        else:
            values["susertoken"] = self.__parent._token

        values["source"] = self.__parent._access_type.value

        self.__parent._ws_send(values)
        self.__parent.IS_CONNECTED.set()

    def send_user_specific_ping(self, transport):
        logger.debug("sending ping")
        transport.send_ping(message=self.__ping_msg)

    def on_ws_frame(self, transport: WSTransport, frame: WSFrame) -> None:
        if frame.msg_type == WSMsgType.TEXT:
            msg = frame.get_payload_as_utf8_text()
            self.__parent.on_data_callback(msg)
            return
        if frame.msg_type == WSMsgType.PONG:
            # logger.info(frame)
            # self.parent._pong_event.set()
            transport.notify_user_specific_pong_received()
        elif frame.msg_type == WSMsgType.CLOSE:
            close_msg = frame.get_close_message()
            close_code = frame.get_close_code()
            if close_msg:
                close_msg = close_msg.decode()
            if close_code == 1008:
                self.__parent._disconnect_socket = True
                close_msg = "Invalid credentials."
            logger.info(
                f"Shoonya Ticker disconnected, code={close_code}, reason={close_msg}"
            )
            transport.disconnect()
        else:
            logger.info(
                f"Shoonya is expected to send text messages, instead received {frame.msg_type}"
            )

    def on_ws_disconnected(self, transport: WSTransport) -> None:
        if self.__parent._on_close:
            self.__loop.create_task(self.__parent._on_close())
        if self.__parent._disconnect_socket:
            self.__parent._initiate_shutdown()
        else:
            logger.info("Trying to reconnect..")
            transport.disconnect()
            self.__loop.create_task(self.__parent.start_ticker(reconnect=True))
