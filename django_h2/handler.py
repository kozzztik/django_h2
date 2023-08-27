import logging
import typing as t
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

    def __init__(self, root_path=""):
        self.root_path = root_path
        if settings.FORCE_SCRIPT_NAME:
            self.root_path = settings.FORCE_SCRIPT_NAME
        self.static_handler = None
        self.load_middleware(False)

    def handle_request(self, request: H2Request) -> HttpResponse:
        # Request is complete and can be served.
        set_script_prefix(self.root_path)
        signals.request_started.send(sender=self.__class__, scope=request.scope)
        response = self.get_response(request)
        response._handler_class = self.__class__
        return response


class StaticHandler(StaticFilesHandlerMixin):
    def __init__(self):
        self.base_url = urlparse(self.get_base_url())

    def handle_request(self, request: H2Request):
        if self._should_handle(request.path):
            return self.get_response(request)
        return None
