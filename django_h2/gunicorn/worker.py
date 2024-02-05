import asyncio
from datetime import datetime
import os
import sys
import io
import traceback

from gunicorn.workers.base import Worker
from gunicorn.sock import ssl_context as gconf_ssl_context

from django_h2.protocol import RequestContext
from django_h2.server import Server, FallbackServer
from django_h2.utils import configure_ssl_context
from django_h2 import signals


class H2Worker(Worker):  # TODO max requests
    server: Server = None
    loop: asyncio.AbstractEventLoop = None

    def run(self):
        servers = []
        ssl_context = self.get_ssl_context()
        for s in self.sockets:
            coro = self.loop.create_server(
                self.server.protocol_factory,
                sock=s,
                ssl=ssl_context)
            servers.append(self.loop.run_until_complete(coro))
        notify_task = self.loop.create_task(self.notify_task())

        try:
            self.loop.run_forever()
        except KeyboardInterrupt:
            pass

        notify_task.cancel()
        # Close the server
        for server in servers:
            server.close()
            self.loop.run_until_complete(server.wait_closed())
        self.loop.close()

    async def notify_task(self):
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
            self.server = FallbackServer(self.loop, tb_string.getvalue())
            return
        script_name = os.environ.get("SCRIPT_NAME", "")
        self.server = Server(
            self.loop,
            serve_static=self.cfg.serve_static,
            max_workers=self.cfg.threads,
            root_path=script_name or "",
        )
        signals.pre_request.connect(self.pre_request)
        signals.post_request.connect(self.post_request)
        signals.request_exception.connect(self.request_exc)

    def get_ssl_context(self):
        if not self.cfg.is_ssl:
            return None
        context = gconf_ssl_context(self.cfg)
        context = configure_ssl_context(context)
        return context

    def pre_request(self, sender: RequestContext, **_):
        self.cfg.pre_request(self, sender.request)

    def post_request(self, sender: RequestContext, response, **_):
        request_time = datetime.now() - sender.start_time
        request = sender.request
        # TODO make better logging
        response.status = response.status_code  # wsgi logging compatibility
        try:
            self.log.access(response, request, request.META, request_time)
            self.cfg.post_request(self, request, request.META, response)
        except Exception as exc:
            self.log.exception("Exception in post_request hook %s", exc)

    def request_exc(self, sender, exc, **_):
        self.log.exception(exc)
