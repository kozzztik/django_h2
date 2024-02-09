from concurrent.futures.thread import ThreadPoolExecutor

from django.http import HttpResponse

from django_h2.handler import H2Handler, StaticHandler
from django_h2.protocol import DjangoH2Protocol, Stream
from django_h2 import signals


class Server:
    # pylint: disable=too-many-arguments
    def __init__(
            self, loop, max_workers=None, serve_static=False, root_path="",
            logger=None):
        self.handler = H2Handler()
        self.static_handler = None
        if serve_static:
            self.static_handler = StaticHandler()
        self.thread_pool = ThreadPoolExecutor(max_workers=max_workers)
        self.root_path = root_path
        self.loop = loop
        self.logger = logger

    def protocol_factory(self):
        return DjangoH2Protocol(self, logger=self.logger)

    async def handle_request(self, stream: Stream):
        try:
            signals.pre_request.send(stream)
            response = None
            if self.static_handler:
                response = self.static_handler.handle_request(stream.request)
            if not response:
                response = await self.loop.run_in_executor(
                    self.thread_pool,
                    self.handler.handle_request,
                    stream.request)
            await stream.send_response(response)
            signals.post_request.send(stream, response=response)
        except Exception as e:
            signals.request_exception.send(stream, exc=e)
        finally:
            stream.end_stream()


class FallbackServer(Server):
    def __init__(self, loop, error_message: str, logger=None):
        self.error_message = error_message.encode("utf-8")
        super().__init__(loop, logger=logger)

    async def handle_request(self, stream: Stream):
        await stream.send_response(HttpResponse(
            status=500, reason="Internal Server Error",
            content_type="text/plain", content=self.error_message
        ))
