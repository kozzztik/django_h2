import asyncio
import logging
import threading
from typing import Callable
from concurrent.futures.thread import ThreadPoolExecutor
from urllib.parse import urlparse

from django.conf import settings
from django.core.handlers.base import BaseHandler
from django.core import signals as dj_signals
from django.urls import set_script_prefix
from django.http import HttpResponse
from django.contrib.staticfiles.handlers import StaticFilesHandlerMixin

from django_h2.request import H2Request
from django_h2 import signals

logger = logging.getLogger("django.request")


class AbstractHandler:
    def __init__(self):
        self.connections = set()

    def graceful_shutdown(self):
        for conn in self.connections:
            conn.graceful_shutdown()

    async def handle_request(self, request: H2Request, send: Callable):
        raise NotImplementedError()


class H2Handler(BaseHandler, AbstractHandler):
    def __init__(self, loop, root_path="", max_workers=None, timeout=None):
        self.timeout = timeout
        self.loop = loop
        self.root_path = root_path
        if settings.FORCE_SCRIPT_NAME:
            self.root_path = settings.FORCE_SCRIPT_NAME
        self.thread_pool = ThreadPoolExecutor(max_workers=max_workers)
        self.load_middleware(False)
        super().__init__()

    def _inner_handle_request(
            self,
            request: H2Request,
            response_future: asyncio.Future,
            close_event: threading.Event):
        try:
            # Request is complete and can be served.
            set_script_prefix(self.root_path)
            dj_signals.request_started.send(
                sender=self.__class__, environ=request.META, request=request)
            response = self.get_response(request)
            # pylint: disable=protected-access
            response._handler_class = self.__class__
        except BaseException as e:
            self.loop.call_soon_threadsafe(response_future.set_exception, e)
            return
        self.loop.call_soon_threadsafe(response_future.set_result, response)
        try:
            close_event.wait(self.timeout)
        finally:
            response.close()

    async def handle_request(self, request: H2Request, send: Callable):
        response_future = asyncio.Future()
        close_event = threading.Event()
        task = self.loop.run_in_executor(
            self.thread_pool,
            self._inner_handle_request,
            request,
            response_future,
            close_event
        )
        try:
            try:
                async with asyncio.timeout(self.timeout):
                    response = await response_future
            except BaseException as e:
                signals.request_exception.send(
                    self.__class__, request=request, exc=e)
                response = HttpResponse(
                    status=504 if isinstance(e, asyncio.TimeoutError) else 500
                )
            if response.streaming:
                # free worker thread so it wouldn't stuck while streaming
                close_event.set()
            await send(response)
        finally:
            signals.request_finished.send_robust(
                self.__class__, request=request, response=response)
            close_event.set()
        await task  # await task so all exceptions will raise here


class StaticHandler(StaticFilesHandlerMixin, AbstractHandler):
    def __init__(self, handler: H2Handler):
        self.handler = handler
        self.base_url = urlparse(self.get_base_url())
        super().__init__()

    async def handle_request(self, request: H2Request, send: Callable):
        if self._should_handle(request.path):
            await send(self.get_response(request))
        else:
            await self.handler.handle_request(request, send)


class FallbackHandler(AbstractHandler):
    def __init__(self, error_message: str):
        self.error_message = error_message.encode("utf-8")
        super().__init__()

    async def handle_request(self, request: H2Request, send: Callable):
        await send(
            HttpResponse(
                status=500, reason="Internal Server Error",
                content_type="text/plain", content=self.error_message
            )
        )
