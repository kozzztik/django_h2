import asyncio

from django.test import client as django_client
from django.core.handlers.wsgi import LimitedStream
from django_h2 import sse


class AsyncClientHandler(django_client.AsyncClientHandler):
    async def get_response_async(self, request):
        if isinstance(request._stream, django_client.FakePayload):
            # that is a patch for django async client that for some reason
            # fails on testing multipart data, as FakePayload does not support
            # read 64k parts as "it is behind limit"
            request._stream = LimitedStream(
                request._stream,
                len(request._stream)
            )
        protocol = None
        if 'HTTP_H2_PROTOCOL' in request.META:
            request.h2_stream_id = 0
            protocol = request.h2_protocol = SSEH2ProtocolMock()
        response = await super().get_response_async(request)
        if protocol:
            response.events = protocol
            if isinstance(response, sse.SSEResponse):
                protocol.handle(response)
        return response

class SSEH2ProtocolMock:
    timeout = 15

    def __init__(self):
        self.finished = asyncio.Future()
        self.queue = asyncio.Queue()
        self.task = None

    def end_stream(self, *args):
        pass

    def handle(self, response: sse.SSEResponse):
        self.task = asyncio.create_task(response.handler)

    def close(self):
        if self.task:
            self.task.cancel()
            self.task = None
        self.finished.set_result(True)

    async def send_data(self, data: bytes, *args, **kwargs):
        data = data.decode('utf-8')
        event = {}
        for s in data.split('\n'):
            if not s:
                continue
            name, value = s.split(': ', 1)
            event[name] = value
        await self.queue.put(event)

    async def __aiter__(self):
        return self

    async def __anext__(self):
        while not self.finished.done():
            try:
                return self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            task = asyncio.create_task(self.queue.get())
            done, _ = await asyncio.wait(
                [task, self.finished],
                return_when=asyncio.FIRST_COMPLETED,
                timeout=self.timeout
            )
            if task in done:
                return task.result()
        raise StopIteration()


class AsyncClient(django_client.AsyncClient):
    def __init__(
        self,
        enforce_csrf_checks=False,
        raise_request_exception=True,
        *,
        headers=None,
        **defaults,
    ):
        super().__init__(headers=headers, **defaults)
        self.handler = AsyncClientHandler(enforce_csrf_checks)
        self.raise_request_exception = raise_request_exception
        self.exc_info = None
        self.extra = None
        self.headers = None

    async def sse(self, path, data=None, secure=False, **extra):
        return await self.get(
            path, data=data, secure=secure, h2_protocol="1", **extra)

