import asyncio
from datetime import datetime
import os
import sys
import io
import traceback

from django.core import signals as dj_signals
from gunicorn.workers.base import Worker
from gunicorn.sock import ssl_context as gconf_ssl_context

from django_h2 import handler
from django_h2.protocol import DjangoH2Protocol
from django_h2.request import H2Request
from django_h2.utils import configure_ssl_context
from django_h2 import signals


class H2Worker(Worker):
    handler = None
    loop: asyncio.AbstractEventLoop = None
    script_name = ''
    protocol_logger = None
    servers = None
    notify_task = None

    def protocol_factory(self):
        return DjangoH2Protocol(
            self.handler,
            logger=self.protocol_logger,
            root_path=self.script_name
        )

    def close(self):
        if self.notify_task and not self.notify_task.done():
            self.notify_task.cancel()
            self.notify_task = None
        if self.servers is not None:
            for server in self.servers:
                server.close()
                self.loop.run_until_complete(server.wait_closed())
            self.servers = None

    def run(self):
        self.servers = []
        ssl_context = self.get_ssl_context()
        for s in self.sockets:
            coro = self.loop.create_server(
                self.protocol_factory,
                sock=s,
                ssl=ssl_context)
            self.servers.append(self.loop.run_until_complete(coro))
        signals.server_started.send(self.__class__, handler=self.handler)
        self.notify_task = self.loop.create_task(self.notify_coro())

        try:
            self.loop.run_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.close()
            self.loop.close()

    async def notify_coro(self):
        while True:
            self.notify()
            await asyncio.sleep(1)

    def load_wsgi(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        try:
            self.app.wsgi()  # init django app
        except Exception as e:
            if not self.cfg.reload:
                raise
            self.log.exception(e)
            # if loading failed, provide server that will tell exception
            tb_string = io.StringIO()
            traceback.print_tb(sys.exc_info()[2], file=tb_string)
            self.handler = handler.FallbackHandler(tb_string.getvalue())
            return
        self.script_name = os.environ.get("SCRIPT_NAME", "")
        self.handler = handler.H2Handler(
            self.loop,
            max_workers=self.cfg.threads,
        )
        if self.cfg.serve_static:
            self.handler = handler.StaticHandler(self.handler)
        # as django internals like test client use this signal too and not
        # provide needed context, connect only to handler signals
        dj_signals.request_started.connect(
            self.pre_request, self.handler.__class__)
        signals.request_finished.connect(self.post_request)
        signals.request_exception.connect(self.request_exc)

    def get_ssl_context(self):
        if not self.cfg.is_ssl:
            return None
        context = gconf_ssl_context(self.cfg)
        context = configure_ssl_context(context)
        return context

    def pre_request(self, request: H2Request, **_):
        self.cfg.pre_request(self, request)

    def post_request(self, request: H2Request, response, **_):
        self.nr += 1
        if self.nr >= self.max_requests and self.alive:
            self.alive = False
            self.log.info("Autorestarting worker after current request.")
            self.handler.graceful_shutdown()
            self.loop.stop()
        request_time = datetime.now() - request.stream.start_time
        # TODO make better logging
        response.status = response.status_code  # wsgi logging compatibility
        try:
            self.log.access(response, request, request.META, request_time)
            self.cfg.post_request(self, request, request.META, response)
        except Exception as exc:
            self.log.exception("Exception in post_request hook %s", exc)

    def request_exc(self, sender, exc, **_):
        self.log.exception(exc)
