import asyncio
import logging
import socket
import time
from importlib.resources import files

from django.core.management.commands import runserver as dj_runserver
from django.conf import settings
from django.http import HttpResponse

from django_h2.handler import H2Handler, StaticHandler
from django_h2.utils import configure_ssl_context
from django_h2.protocol import Stream, DjangoH2Protocol
from django_h2 import signals

logger = logging.getLogger('django.server')


class H2ManagementRunServer:
    handler = None

    def __init__(self, server_address, handler, ipv6):
        self.server_address = server_address
        self.ipv6 = ipv6

    def set_app(self, wsgi_handler):
        pass

    def protocol_factory(self):
        return DjangoH2Protocol(self.handler)

    def serve_forever(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ssl_ctx = self.get_ssl_context()
        self.handler = StaticHandler(H2Handler(loop, max_workers=1))
        signals.post_request.connect(log_response)
        signals.request_exception.connect(log_exception)
        coro = loop.create_server(
            self.protocol_factory,
            host=self.server_address[0],
            port=self.server_address[1],
            family=socket.AF_INET6 if self.ipv6 else socket.AF_UNSPEC,
            ssl=ssl_ctx)
        server = loop.run_until_complete(coro)
        signals.server_started.send(self.handler)

        # Serve requests until Ctrl+C is pressed
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            pass

        # Close the server
        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.close()
        signals.post_request.disconnect(log_response)
        signals.request_exception.disconnect(log_exception)

    @staticmethod
    def get_ssl_context():
        ctx = configure_ssl_context()
        ssl_settings = getattr(settings, 'SSL', {})
        crt_file = ssl_settings.get('cert')
        keyfile = ssl_settings.get('key')
        if crt_file and keyfile:
            ctx.load_cert_chain(certfile=crt_file, keyfile=keyfile)
        else:
            if not crt_file:
                crt_file = str(files('django_h2').joinpath('default.crt'))
            ctx.load_cert_chain(certfile=crt_file)
        # TODO
        # ctx.load_verify_locations(cafile='server_ca.pem')
        return ctx


def log_date_time_string():
    """Return the current time formatted for logging."""
    now = time.time()
    year, month, day, hh, mm, ss, _, _, _ = time.localtime(now)
    # pylint: disable=consider-using-f-string
    s = "%02d/%3s/%04d %02d:%02d:%02d" % (
            day, monthname[month], year, hh, mm, ss)
    return s


monthname = [None,
             'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
             'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def log_response(sender: Stream, response: HttpResponse, **_):
    extra = {
        "request": sender.request,
        "server_time": log_date_time_string(),
        "status_code": response.status_code
    }
    if response.status_code >= 500:
        level = logger.error
    elif response.status_code >= 400:
        level = logger.warning
    else:
        level = logger.info
    level(
        "%s %s %s %s",
        sender.request.path, "HTTP/2",
        response.status_code, sender.bytes_send,
        extra=extra)


def log_exception(sender: Stream, exc, **_):
    extra = {
        "request": sender.request,
        "server_time": log_date_time_string(),
        "status_code": 500
    }
    logger.exception(
        "%s %s %s %s",
        sender.request.path, "HTTP/2", 500, str(exc),
        extra=extra)


def patch():
    dj_runserver.Command.server_cls = H2ManagementRunServer
    dj_runserver.Command.protocol = 'https'
