import asyncio
import logging
import socket

from django.core.management.commands import runserver as dj_runserver

from django_h2.server import Server


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
        app_server = Server(loop, serve_static=True, max_workers=1)
        coro = loop.create_server(
            app_server.protocol_factory,
            host=self.server_address[0],
            port=self.server_address[1],
            family=socket.AF_INET6 if self.ipv6 else socket.AF_UNSPEC,
            ssl=app_server.ssl_context)
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


def patch():
    dj_runserver.Command.server_cls = H2ManagementRunServer
    dj_runserver.Command.protocol = 'https'
