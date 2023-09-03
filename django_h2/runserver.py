import asyncio
import logging
import socket
import time

from django.core.management.commands import runserver as dj_runserver
from django.conf import settings
from django.http import HttpResponse

from django_h2.server import Server
from django_h2.utils import configure_ssl_context
from django_h2.handler import H2Request
from django_h2 import signals

logger = logging.getLogger('django.server')


class H2ManagementRunServer:
    def __init__(self, server_address, handler, ipv6):
        self.server_address = server_address
        self.ipv6 = ipv6

    def set_app(self, wsgi_handler):
        pass

    def serve_forever(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ssl_ctx = self.get_ssl_context()
        app_server = Server(loop, serve_static=True, max_workers=1)
        signals.post_request.connect(log_response)
        signals.request_exception.connect(log_exception)
        coro = loop.create_server(
            app_server.protocol_factory,
            host=self.server_address[0],
            port=self.server_address[1],
            family=socket.AF_INET6 if self.ipv6 else socket.AF_UNSPEC,
            ssl=ssl_ctx)
        server = loop.run_until_complete(coro)

        # Serve requests until Ctrl+C is pressed
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            pass

        # Close the server
        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.close()

    @staticmethod
    def get_ssl_context():
        ctx = configure_ssl_context()
        ssl_settings = getattr(settings, 'SSL', {})
        crt_file = ssl_settings.get('cert')
        keyfile = ssl_settings.get('key')
        ctx.load_cert_chain(certfile=crt_file, keyfile=keyfile)
        # ctx.load_verify_locations(cafile='server_ca.pem')
        return ctx


def log_date_time_string():
    """Return the current time formatted for logging."""
    now = time.time()
    year, month, day, hh, mm, ss, x, y, z = time.localtime(now)
    s = "%02d/%3s/%04d %02d:%02d:%02d" % (
            day, monthname[month], year, hh, mm, ss)
    return s


monthname = [None,
             'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
             'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def log_response(sender: H2Request, response: HttpResponse, **_):
    extra = {
        "request": sender,
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
        sender.path, "HTTP/2", response.status_code, sender.h2_bytes_send,
        extra=extra)


def log_exception(sender: H2Request, exc, **_):
    extra = {
        "request": sender,
        "server_time": log_date_time_string(),
        "status_code": 500
    }
    logger.exception(
        "%s %s %s %s",
        sender.path, "HTTP/2", 500, str(exc),
        extra=extra)


def patch():
    dj_runserver.Command.server_cls = H2ManagementRunServer
    dj_runserver.Command.protocol = 'https'
