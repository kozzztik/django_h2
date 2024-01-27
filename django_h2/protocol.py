import asyncio
from typing import List, Tuple, Dict, Awaitable

from h2.errors import ErrorCodes
from django.conf import settings
from django.http import FileResponse, HttpResponse

from django_h2.base_protocol import H2Protocol
from django_h2.request import H2Request


class H2StreamingResponse(HttpResponse):
    handler: Awaitable


class DjangoH2Protocol(H2Protocol):
    stream_data: Dict[int, H2Request]
    chunk_size = 2**16

    def __init__(self, server):
        super().__init__()
        self.stream_data = {}
        self.server = server
        self.max_size = settings.FILE_UPLOAD_MAX_MEMORY_SIZE

    def request_received(self, headers: List[Tuple[str, str]], stream_id: int):
        self.stream_data[stream_id] = H2Request(
            self, stream_id, headers, root_path=self.server.root_path)

    def receive_data(self, data: bytes, stream_id: int):
        try:
            request = self.stream_data[stream_id]
        except KeyError:
            self.conn.reset_stream(
                stream_id, error_code=ErrorCodes.PROTOCOL_ERROR
            )
            return
        stream = request._stream
        if stream.tell() + len(data) > self.max_size:
            self.conn.reset_stream(
                stream_id, error_code=ErrorCodes.REFUSED_STREAM
            )
            return
        stream.write(data)

    def stream_complete(self, stream_id: int):
        """
        When a stream is complete, we can send our response.
        """
        try:
            request = self.stream_data[stream_id]
        except KeyError:
            return  # Just return, we probably 405'd this already
        request.stream_complete()
        request.h2_task = asyncio.create_task(
            self.server.handle_request(self, stream_id, request))

    def connection_lost(self, exc):
        while self.stream_data:
            _, request = self.stream_data.popitem()
            if request.h2_task and not request.h2_task.done():
                request.h2_task.cancel()
        super().connection_lost(exc)

    def stream_reset(self, stream_id: int):
        """
        A stream reset was sent. Stop sending data.
        """
        request = self.stream_data.pop(stream_id)
        if request.h2_task:
            request.h2_task.cancel()
        super().stream_reset(stream_id)

    def end_stream(self, stream_id: int):
        """ Gracefully close stream """
        if self.stream_data.pop(stream_id, None):
            super().end_stream(stream_id)

    async def send_data(
            self, data: bytes, stream_id: int, end_stream: bool = True):
        try:
            request = self.stream_data[stream_id]
        except KeyError:
            return  # Just return, we probably 405'd this already
        await super().send_data(data, stream_id, end_stream=end_stream)
        request.h2_bytes_send += len(data)

    async def send_response(self, stream_id: int, response: HttpResponse):
        """Encode and send a response out over ASGI."""
        # Increase chunk size on file responses (ASGI servers handles low-level
        # chunking).
        if isinstance(response, FileResponse):
            response.block_size = self.chunk_size
        response_headers = [
            (':status', str(response.status_code)),
            *response.items()
        ]
        # Collect cookies into headers. Have to preserve header case as there
        # are some non-RFC compliant clients that require e.g. Content-Type.
        for c in response.cookies.values():
            response_headers.append(
                ("Set-Cookie", c.output(header=""))
            )
        self.conn.send_headers(stream_id, response_headers)
        self.transport.write(self.conn.data_to_send())

        if isinstance(response, H2StreamingResponse):
            await response.handler
            return 0
        # Streaming responses need to be pinned to their iterator.
        if response.streaming:
            # Access `__iter__` and not `streaming_content` directly in case
            # it has been overridden in a subclass.
            for part in response:
                await self.send_data(part, stream_id, end_stream=False)
        else:
            await self.send_data(response.content, stream_id, end_stream=False)
        self.end_stream(stream_id)
        response.close()
