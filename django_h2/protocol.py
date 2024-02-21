import asyncio
import io
from contextlib import aclosing
from typing import List, Tuple

from h2.errors import ErrorCodes
from hyperframe.frame import GoAwayFrame
from django.conf import settings
from django.http import HttpResponse

from django_h2 import signals
from django_h2.base_protocol import BaseH2Protocol, BaseStream
from django_h2.request import H2Request


class Stream(BaseStream):
    request: H2Request
    task: asyncio.Task | None = None
    bytes_received = 0
    streaming = False
    _close_waiter: asyncio.Future | None = None

    def __init__(
            self,
            protocol: BaseH2Protocol,
            stream_id: int,
            headers: List[Tuple[str, str]]):
        super().__init__(protocol, stream_id, headers)
        self.request = H2Request(self, headers, self.protocol.root_path)
        self._max_request_size = settings.FILE_UPLOAD_MAX_MEMORY_SIZE
        self.request_body = io.BytesIO()
        signals.stream_started.send(self.__class__, stream=self)

    def close(self, exc=None):
        if self._close_waiter:
            self._close_waiter.set_result(self)
            self._close_waiter = None
        if self.task and not self.task.done():
            self.task.cancel()
            self.task = None
        if exc:
            signals.request_exception.send(
                self.__class__, request=self.request, exc=exc)
        super().close()

    def __await__(self):
        self._close_waiter = asyncio.Future()
        return self._close_waiter.__await__()

    def event_receive_data(self, data: bytes):
        if self.bytes_received + len(data) > self._max_request_size:
            self.conn.reset_stream(
                self.stream_id, error_code=ErrorCodes.REFUSED_STREAM
            )
            self.transport.write(self.conn.data_to_send())
            self.protocol.stream_reset(self.stream_id)
            return
        self.request_body.write(data)
        self.bytes_received += len(data)
        self.conn.increment_flow_control_window(len(data))
        self.transport.write(self.conn.data_to_send())

    def event_stream_complete(self):
        self.request.stream_complete(self.request_body)
        self.request_body = None  # to free memory
        self.task = asyncio.create_task(self.handle_task())

    async def handle_task(self):
        try:
            await self.protocol.handler.handle_request(
                self.request, self.send_response)
        except asyncio.CancelledError:
            raise  # so asyncio know that context closed correctly
        except BaseException as e:
            # do not raise and log
            signals.request_exception.send(
                self.__class__, request=self.request, exc=e)
        finally:
            self.close()

    async def send_response(self, response: HttpResponse):
        response_headers = [
            (':status', str(response.status_code)),
            *response.items()
        ]
        # Collect cookies into headers. Have to preserve header case as there
        # are some non-RFC compliant clients that require e.g. Content-Type.
        # However, H2 will normalize it anyway.
        for c in response.cookies.values():
            response_headers.append(("Set-Cookie", c.output(header="")))
        self.send_headers(response_headers)
        # Streaming responses need to be pinned to their iterator.
        if response.streaming:
            self.streaming = True
            try:
                if self.protocol.alive:
                    # - Consume via `__aiter__` and not `streaming_content`
                    #   directly, to allow mapping of a sync iterator.
                    # - Use aclosing() when consuming aiter.
                    #   See https://github.com/python/cpython/commit/6e8dcda
                    async with aclosing(aiter(response)) as content:
                        async for part in content:
                            await self.send_data(part, end_stream=False)
            finally:
                self.end_stream()
        else:
            await self.send_data(response.content, end_stream=True)


class DjangoH2Protocol(BaseH2Protocol):
    stream_class = Stream

    def __init__(self, handler, logger=None, root_path=''):
        super().__init__(logger=logger)
        self.handler = handler
        self.root_path = root_path
        self.conn.local_settings.initial_window_size = (
            settings.FILE_UPLOAD_MAX_MEMORY_SIZE)
        self.conn.local_settings.acknowledge()
        handler.connections.add(self)  # TODO: limit max connections
        self.alive = True

    def connection_lost(self, exc):
        super().connection_lost(exc)
        self.handler.connections.remove(self)

    def graceful_shutdown(self):
        last_stream_id = self.conn.highest_inbound_stream_id
        # send go away frame directly, as h2 on "close connection" changes its
        # state to "closed" where data can't be sent on already opened streams
        f = GoAwayFrame(stream_id=0, last_stream_id=last_stream_id)
        self.transport.write(f.serialize())
        self.alive = False
        # build a list to avoid "dict values changed" while iterating
        for stream in [s for s in self.streams.values() if s.streaming]:
            stream.task.cancel()
        # this task will close connection after all streams finish
        asyncio.create_task(self.wait_shutdown())

    async def wait_shutdown(self):
        while self.streams:
            # wait first stream in list. Wait next one on next while iteration
            await next(iter(self.streams.values()))
        self.transport.close()

    def request_received(self, headers: List[Tuple[str, str]], stream_id: int):
        # RFC 7540 6.8 https://httpwg.org/specs/rfc7540.html#GOAWAY
        # Once (GoAway) sent, the sender will ignore frames sent on streams
        # initiated by the receiver if the stream has an identifier higher
        # than the included last stream identifier.
        if self.alive:
            super().request_received(headers, stream_id)
