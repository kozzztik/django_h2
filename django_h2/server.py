import logging
from concurrent.futures.thread import ThreadPoolExecutor

from django_h2.handler import H2Handler, StaticHandler, H2Request
from django_h2.protocol import DjangoH2Protocol
from django_h2 import signals

logger = logging.getLogger("django.server")


class Server:
    def __init__(
            self, loop, max_workers=None, serve_static=False, root_path=""):
        self.handler = H2Handler()
        self.static_handler = None
        if serve_static:
            self.static_handler = StaticHandler()
        self.thread_pool = ThreadPoolExecutor(max_workers=max_workers)
        self.root_path = root_path
        self.loop = loop

    def protocol_factory(self):
        return DjangoH2Protocol(self)

    async def handle_request(
            self,
            protocol: DjangoH2Protocol,
            stream_id: int,
            request: H2Request):
        try:
            signals.pre_request.send(request)
            response = None
            if self.static_handler:
                response = self.static_handler.handle_request(request)
            if not response:
                response = await self.loop.run_in_executor(
                    self.thread_pool, self.handler.handle_request, request)
            await protocol.send_response(stream_id, response)
            signals.post_request.send(request, response=response)
        except Exception as e:
            signals.request_exception.send(request, exc=e)
        finally:
            protocol.end_stream(stream_id)
