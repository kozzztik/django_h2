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
import collections
import datetime
from typing import List, Tuple, Dict

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (
    ConnectionTerminated, DataReceived, RemoteSettingsChanged,
    RequestReceived, StreamEnded, StreamReset, WindowUpdated
)
from h2.exceptions import ProtocolError, StreamClosedError
from h2.settings import SettingCodes


RequestData = collections.namedtuple('RequestData', ['headers', 'data'])


class H2Protocol(asyncio.Protocol):
    flow_control_futures: Dict[int, asyncio.Future] = None

    def __init__(self):
        config = H2Configuration(client_side=False, header_encoding='utf-8')
        self.conn = H2Connection(config=config)
        self.transport = None
        self.flow_control_futures = {}

    def connection_made(self, transport: asyncio.Transport):
        self.transport = transport
        self.conn.initiate_connection()
        self.transport.write(self.conn.data_to_send())

    def connection_lost(self, exc):
        for future in self.flow_control_futures.values():
            future.cancel()
        self.flow_control_futures = {}

    def data_received(self, data: bytes):
        try:
            events = self.conn.receive_data(data)
        except ProtocolError as e:
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
        raise NotImplementedError()

    def stream_complete(self, stream_id: int):
        raise NotImplementedError()

    def receive_data(self, data: bytes, stream_id: int):
        raise NotImplementedError()

    def stream_reset(self, stream_id: int):
        """
        A stream reset was sent. Stop sending data.
        """
        if stream_id in self.flow_control_futures:
            future = self.flow_control_futures.pop(stream_id)
            future.cancel()

    async def send_data(
            self, data: bytes, stream_id: int, end_stream: bool = True):
        """
        Send data according to the flow control rules.
        """
        if not data and end_stream:
            self.end_stream(stream_id)
        while data:
            while self.conn.local_flow_control_window(stream_id) < 1:
                try:
                    await self.wait_for_flow_control(stream_id)
                except asyncio.CancelledError:
                    return

            chunk_size = min(
                self.conn.local_flow_control_window(stream_id),
                len(data),
                self.conn.max_outbound_frame_size,
            )

            try:
                self.conn.send_data(
                    stream_id,
                    data[:chunk_size],
                    end_stream=end_stream and (chunk_size == len(data))
                )
            except (StreamClosedError, ProtocolError):
                # The stream got closed, and we didn't get told. We're done
                # here.
                break

            self.transport.write(self.conn.data_to_send())
            data = data[chunk_size:]

    async def wait_for_flow_control(self, stream_id: int):
        """
        Waits for a Future that fires when the flow control window is opened.
        """
        f = asyncio.Future()
        self.flow_control_futures[stream_id] = f
        await f

    def window_updated(self, stream_id: int, delta: int):
        """
        A window update frame was received. Unblock some number of flow control
        Futures.
        """
        if stream_id and stream_id in self.flow_control_futures:
            f = self.flow_control_futures.pop(stream_id)
            f.set_result(delta)
        elif not stream_id:
            for f in self.flow_control_futures.values():
                f.set_result(delta)

            self.flow_control_futures = {}

    def end_stream(self, stream_id: int):
        self.conn.end_stream(stream_id)
        self.transport.write(self.conn.data_to_send())

    def send_headers(self, stream_id, headers):
        self.conn.send_headers(stream_id, headers)
        self.transport.write(self.conn.data_to_send())


class StreamContext:
    bytes_send = 0
    start_time: datetime.datetime = None
    protocol: H2Protocol = None
    stream_id: int = None

    def __init__(self, protocol: H2Protocol, stream_id: int):
        self.protocol = protocol
        self.stream_id = stream_id
        self.start_time = datetime.datetime.now()
        self.transport = protocol.transport

    async def send_data(self, data: bytes, end_stream: bool = True):
        await self.protocol.send_data(
            data, self.stream_id, end_stream=end_stream)
        self.bytes_send += len(data)

    def end_stream(self):
        self.protocol.end_stream(self.stream_id)

    def close(self):
        pass
