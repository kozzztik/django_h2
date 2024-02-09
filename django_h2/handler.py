import logging
from concurrent.futures.thread import ThreadPoolExecutor
from urllib.parse import urlparse

from django.conf import settings
from django.core.handlers.base import BaseHandler
from django.core import signals
from django.urls import set_script_prefix
from django.http import HttpResponse
from django.contrib.staticfiles.handlers import StaticFilesHandlerMixin

from django_h2.request import H2Request


logger = logging.getLogger("django.request")


class H2Handler(BaseHandler):
    request_class = H2Request

    def __init__(self, loop, root_path="", max_workers=None):
        self.loop = loop
        self.root_path = root_path
        if settings.FORCE_SCRIPT_NAME:
            self.root_path = settings.FORCE_SCRIPT_NAME
        self.thread_pool = ThreadPoolExecutor(max_workers=max_workers)
        self.load_middleware(False)

    def _inner_handle_request(self, request: H2Request) -> HttpResponse:
        # Request is complete and can be served.
        set_script_prefix(self.root_path)
        signals.request_started.send(
            sender=self.__class__, environ=request.META)
        response = self.get_response(request)
        response._handler_class = self.__class__
        return response

    async def handle_request(self, request: H2Request) -> HttpResponse:
        return await self.loop.run_in_executor(
            self.thread_pool,
            self._inner_handle_request,
            request
        )


class StaticHandler(StaticFilesHandlerMixin):
    def __init__(self, handler: H2Handler):
        self.handler = handler
        self.base_url = urlparse(self.get_base_url())

    async def handle_request(self, request: H2Request):
        if self._should_handle(request.path):
            return self.get_response(request)
        return await self.handler.handle_request(request)


class FallbackHandler:
    def __init__(self, error_message: str):
        self.error_message = error_message.encode("utf-8")

    async def handle_request(self, request: H2Request) -> HttpResponse:
        return HttpResponse(
            status=500, reason="Internal Server Error",
            content_type="text/plain", content=self.error_message
        )
