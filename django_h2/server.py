import time
import logging
import ssl
from concurrent.futures.thread import ThreadPoolExecutor

from django.conf import settings
from django.http import HttpResponse

from django_h2.handler import H2Handler, StaticHandler, H2Request
from django_h2.protocol import DjangoH2Protocol


logger = logging.getLogger("django.server")


class Server:
    def __init__(
            self, loop, ssl_context=None, max_workers=None,
            serve_static=False, root_path=""):
        if not ssl_context:
            ssl_settings = getattr(settings, 'SSL', {})
            crt_file = ssl_settings.get('cert')
            keyfile = ssl_settings.get('key')
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.options |= (
                    ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1 | ssl.OP_NO_COMPRESSION
            )
            ssl_context.load_cert_chain(certfile=crt_file, keyfile=keyfile)
            ssl_context.set_alpn_protocols(["h2"])
            # ssl_context.load_verify_locations(cafile='server_ca.pem')
        self.ssl_context = ssl_context
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
            try:
                response = None
                if self.static_handler:
                    response = self.static_handler.handle_request(request)
                if not response:
                    response = await self.loop.run_in_executor(
                        self.thread_pool, self.handler.handle_request, request)
                await protocol.send_response(stream_id, response)
                self.log_response(request, response)
            finally:
                protocol.end_stream(stream_id)
        except Exception as e:
            self.log_exception(request, e)

    def log_date_time_string(self):
        """Return the current time formatted for logging."""
        now = time.time()
        year, month, day, hh, mm, ss, x, y, z = time.localtime(now)
        s = "%02d/%3s/%04d %02d:%02d:%02d" % (
                day, self.monthname[month], year, hh, mm, ss)
        return s

    monthname = [None,
                 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

    def log_response(self, request: H2Request, response: HttpResponse):
        extra = {
            "request": request,
            "server_time": self.log_date_time_string(),
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
            request.path, "HTTP/2", response.status_code, request.h2_bytes_send,
            extra=extra)

    def log_exception(self, request: H2Request, exc):
        extra = {
            "request": request,
            "server_time": self.log_date_time_string(),
            "status_code": 500
        }
        logger.exception(
            "%s %s %s %s",
            request.path, "HTTP/2", 500, str(exc),
            extra=extra)
