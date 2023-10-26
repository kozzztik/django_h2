import asyncio
from datetime import datetime
import os

from gunicorn.workers.base import Worker
from gunicorn.sock import ssl_context as gconf_ssl_context

from django_h2.handler import H2Request
from django_h2.server import Server
from django_h2.utils import configure_ssl_context
from django_h2 import signals


class H2Worker(Worker):
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
        self.loop.create_task(self.notify_task())
        # Serve requests until Ctrl+C is pressed
        try:
            self.loop.run_forever()
        except KeyboardInterrupt:
            # TODO maybe something specific for gunicorn
            pass

        # Close the server
        for server in servers:
            server.close()
            self.loop.run_until_complete(server.wait_closed())
        self.loop.close()

    async def notify_task(self):
        self.notify()
        await asyncio.sleep(1)  # TODO

    def load_wsgi(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        script_name = os.environ.get("SCRIPT_NAME", "")
        try:
            self.server = Server(
                self.loop,
                serve_static=True,  # TODO tmp
                max_workers=self.cfg.threads,
                root_path=script_name or "",
            )
        except SyntaxError as e:
            if not self.cfg.reload:
                raise
            self.log.exception(e)
        signals.pre_request.connect(self.pre_request)
        signals.post_request.connect(self.post_request)
        signals.request_exception.connect(self.request_exc)

    def get_ssl_context(self):
        if not self.cfg.is_ssl:
            return None
        context = gconf_ssl_context(self.cfg)
        context = configure_ssl_context(context)
        return context

    def pre_request(self, sender: H2Request, **_):
        self.cfg.pre_request(self, sender)

    def post_request(self, sender: H2Request, response, **_):
        request_time = datetime.now() - sender.start_time
        try:
            self.log.access(response, sender, sender.META, request_time)
            self.cfg.post_request(self, sender, sender.META, response)
        except Exception as exc:
            self.log.exception("Exception in post_request hook")
            print(exc)

    def request_exc(self, request, exc, **_):
        pass
