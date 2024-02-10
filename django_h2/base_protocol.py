"""
asyncio-server.py from
https://github.com/python-hyper/h2/blob/master/examples/asyncio/asyncio-server.py
~~~~~~~~~~~~~~~~~

A fully-functional HTTP/2 server using asyncio. Requires Python 3.5+.

This example demonstrates handling requests with bodies, as well as handling
those without. In particular, it demonstrates the fact that DataReceived may
be called multiple times, and that applications must handle that possibility.
"""
import asyncio
import datetime
from typing import List, Tuple, Dict, Any, Callable

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (
    ConnectionTerminated, DataReceived, RemoteSettingsChanged,
    RequestReceived, StreamEnded, StreamReset, WindowUpdated
)
from h2.errors import ErrorCodes
from h2.exceptions import ProtocolError, StreamClosedError
from h2.settings import SettingCodes


class BaseH2Protocol(asyncio.Protocol):
    streams: Dict[int, Any] = None
    stream_class: Callable

    def __init__(self, logger=None):
        config = H2Configuration(
            client_side=False, header_encoding='utf-8', logger=logger)
        self.conn = H2Connection(config=config)
        self.transport = None
        self.streams = {}

    def connection_made(self, transport: asyncio.Transport):
        self.transport = transport
        self.conn.initiate_connection()
        self.transport.write(self.conn.data_to_send())

    def connection_lost(self, exc):
        for stream in self.streams.values():
            stream.close(exc)
        self.streams = {}

    def data_received(self, data: bytes):
        try:
            events = self.conn.receive_data(data)
        except ProtocolError:
            self.transport.write(self.conn.data_to_send())
            self.transport.close()
        else:
            self.transport.write(self.conn.data_to_send())
            for event in events:
                if isinstance(event, RequestReceived):
                    self.request_received(event.headers, event.stream_id)
                elif isinstance(event, DataReceived):
                    self.receive_data(event.data, event.stream_id)
                elif isinstance(event, StreamEnded):
                    self.stream_complete(event.stream_id)
                elif isinstance(event, ConnectionTerminated):
                    self.transport.close()
                elif isinstance(event, StreamReset):
                    self.stream_reset(event.stream_id)
                elif isinstance(event, WindowUpdated):
                    self.window_updated(event.stream_id, event.delta)
                elif isinstance(event, RemoteSettingsChanged):
                    if SettingCodes.INITIAL_WINDOW_SIZE in event.changed_settings:
                        self.window_updated(None, 0)

                self.transport.write(self.conn.data_to_send())

    def request_received(self, headers: List[Tuple[str, str]], stream_id: int):
        self.streams[stream_id] = self.stream_class(self, stream_id, headers)

    def stream_complete(self, stream_id: int):
        try:
            self.streams[stream_id].event_stream_complete()
        except KeyError:
            return  # Just return, we probably 405'd this already

    def receive_data(self, data: bytes, stream_id: int):
        try:
            self.streams[stream_id].event_receive_data(data)
        except KeyError:
            self.conn.reset_stream(
                stream_id, error_code=ErrorCodes.PROTOCOL_ERROR
            )

    def stream_reset(self, stream_id: int):
        """
        A stream reset was sent. Stop sending data.
        """
        try:
            self.streams.pop(stream_id).close()
        except KeyError:
            pass

    def window_updated(self, stream_id: int, delta: int):
        """
        A window update frame was received. Unblock some number of flow control
        Futures.
        """
        if not stream_id:
            for stream in self.streams.values():
                stream.event_window_updated(delta)
            return
        try:
            self.streams[stream_id].event_window_updated(delta)
        except KeyError:
            pass


class BaseStream:
    stream_id: int = None
    bytes_send = 0
    start_time: datetime.datetime = None
    protocol: BaseH2Protocol = None
    conn: H2Connection = None
    transport: asyncio.Transport = None
    _flow_control_future: asyncio.Future | None = None

    def __init__(
            self,
            protocol: BaseH2Protocol,
            stream_id: int,
            headers: List[Tuple[str, str]]):
        self.protocol = protocol
        self.stream_id = stream_id
        self.conn = protocol.conn
        self.transport = protocol.transport
        self.start_time = datetime.datetime.now()
        self.transport = protocol.transport

    def end_stream(self):
        self.conn.end_stream(self.stream_id)
        self.transport.write(self.conn.data_to_send())
        self.close()
        # TODO check that it is not done by events
        # self.protocol.streams.pop(self.stream_id)

    def close(self, exc=None):
        if self._flow_control_future:
            self._flow_control_future.cancel()
            self._flow_control_future = None

    async def wait_for_flow_control(self) -> int:
        while True:
            window = self.conn.local_flow_control_window(self.stream_id)
            if window > 0:
                return window
            if not self._flow_control_future:
                self._flow_control_future = asyncio.Future()
            await self._flow_control_future

    async def send_data(self, data: bytes, end_stream: bool = True):
        """
        Send data according to the flow control rules.
        """
        if not data and end_stream:
            self.end_stream()
        while data:
            window = await self.wait_for_flow_control()
            chunk_size = min(
                window,
                len(data),
                self.protocol.conn.max_outbound_frame_size,
            )

            try:
                self.conn.send_data(
                    self.stream_id,
                    data[:chunk_size],
                    end_stream=end_stream and (chunk_size == len(data))
                )
            except (StreamClosedError, ProtocolError):
                # The stream got closed, and we didn't get told. We're done
                # here.
                break

            self.transport.write(self.conn.data_to_send())
            data = data[chunk_size:]
            self.bytes_send += chunk_size

    def event_window_updated(self, delta: int):
        """
        A window update frame was received. Unblock some number of flow control
        Futures.
        """
        if self._flow_control_future:
            self._flow_control_future.set_result(delta)
            self._flow_control_future = None

    def send_headers(self, headers):
        self.conn.send_headers(self.stream_id, headers)
        self.transport.write(self.conn.data_to_send())

    def event_stream_complete(self):
        raise NotImplementedError()

    def event_receive_data(self, data: bytes):
        raise NotImplementedError()
