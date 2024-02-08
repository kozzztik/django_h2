import os
import socket
import ssl
from importlib.resources import files
import logging
import tempfile
from unittest import mock

import django
from django import urls
from django.conf import ENVIRONMENT_VARIABLE
from django.http import JsonResponse
from django.core.management import execute_from_command_line
from django.test import override_settings
from h2.exceptions import ProtocolError
import pytest

from tests import empty_settings
from tests.utils import BaseWorkerThread


class CommandWorkerThread(BaseWorkerThread):
    def __init__(self, port):
        self.port = port
        super().__init__()

    def get_server_addr(self):
        return '127.0.0.1', self.port

    def _internal_run(self):
        execute_from_command_line([
            'path', 'runserver', str(self.port),
            '--skip-checks', '--nothreading', '--noreload'
        ])


def error_view(*args):
    raise ValueError('foo')


class UrlConf:
    urlpatterns = [
        urls.re_path(r'^ping/$', lambda x: JsonResponse(dict(x.GET))),
        urls.re_path(r'^error/$', error_view),

    ]


@pytest.fixture(name="django_config")
def django_config_fixture():
    os.environ[ENVIRONMENT_VARIABLE] = empty_settings.__name__
    django.setup()
    with override_settings(
            ROOT_URLCONF=UrlConf,
            STATICFILES_DIRS=[files('django_h2')],
            STATIC_URL='/static/'):
        yield


@pytest.fixture(name="server_port")
def server_port_fixture():
    sock_server = socket.socket()
    sock_server.bind(('127.0.0.1', 0))
    port = sock_server.getsockname()[1]
    sock_server.close()
    return port


@pytest.fixture(name="ssl_context")
def ssl_context_fixture():
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def test_happy_path(django_config, server_port, ssl_context):
    with CommandWorkerThread(server_port) as thread:
        thread.connect_ssl(ssl_context)
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'https'),
            (':method', 'GET'),
            (':path', '/ping/?foo=bar'),
        ])
        assert response.status_code == 200
        assert response.body == b'{"foo": ["bar"]}'
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'https'),
            (':method', 'GET'),
            (':path', '/static/default.crt'),
        ])
    assert response.status_code == 200
    assert response['content-disposition'] == 'inline; filename="default.crt"'
    assert len(response.body) > 0


def test_non_200_responses(django_config, server_port, ssl_context):
    logger = logging.getLogger('django.server')

    with CommandWorkerThread(server_port) as thread:
        thread.connect_ssl(ssl_context)
        with mock.patch.object(logger, 'error') as error_mock:
            response = thread.make_request([
                (':authority', '127.0.0.1'),
                (':scheme', 'https'),
                (':method', 'GET'),
                (':path', '/error/'),
            ])
        assert response.status_code == 500
        assert error_mock.called
        assert len(error_mock.call_args.args) == 5
        assert error_mock.call_args.args[:4] == (
            '%s %s %s %s', '/error/', 'HTTP/2', 500)
        with mock.patch.object(logger, 'warning') as warning_mock:
            response = thread.make_request([
                (':authority', '127.0.0.1'),
                (':scheme', 'https'),
                (':method', 'GET'),
                (':path', '/some_path/'),
            ])
        assert response.status_code == 404
        assert warning_mock.called
        assert len(warning_mock.call_args.args) == 5
        assert warning_mock.call_args.args[:4] == (
            '%s %s %s %s', '/some_path/', 'HTTP/2', 404)


def test_handler_exception(django_config, server_port, ssl_context):
    logger = logging.getLogger('django.server')
    with CommandWorkerThread(server_port) as thread:
        thread.connect_ssl(ssl_context)
        with mock.patch(
                'django_h2.handler.H2Handler.handle_request',
                side_effect=ValueError()) as handler_mock:
            with mock.patch.object(logger, 'error') as error_mock:
                with pytest.raises(ProtocolError):  # TODO better get 500 here
                    thread.make_request([
                        (':authority', '127.0.0.1'),
                        (':scheme', 'https'),
                        (':method', 'GET'),
                        (':path', '/ping/?foo=bar'),
                    ])
    assert handler_mock.called
    assert error_mock.called
    assert len(error_mock.call_args.args) == 5
    assert error_mock.call_args.args[:4] == (
        '%s %s %s %s', '/ping/', 'HTTP/2', 500)


def test_keyboard_interrupt(django_config, server_port, ssl_context):
    class InterruptedThread(CommandWorkerThread):
        async def stopping_task(self, loop):
            raise KeyboardInterrupt()

    with InterruptedThread(server_port) as thread:
        thread.join(5)
    assert not thread.is_alive()


def test_custom_ssl_cert(django_config, server_port, ssl_context):
    with tempfile.TemporaryDirectory() as folder:
        cert_file_name = os.path.join(folder, 'selfsigned.crt')
        key_file_name = os.path.join(folder, 'private.key')
        execute_from_command_line([
            'path', 'gen_cert',
            '--email_address=foo@bar.com',
            '--key_file', key_file_name, '--cert_file', cert_file_name
        ])
        assert os.path.exists(key_file_name)
        assert os.path.exists(cert_file_name)
        with open(cert_file_name, 'r', encoding='utf-8') as f:
            cert_file_data = f.read()
        with override_settings(SSL={
                'cert': cert_file_name,
                'key': os.path.join(folder, 'private.key'),
        }):
            with CommandWorkerThread(server_port) as thread:
                thread.connect_ssl(ssl_context)
                sock = getattr(thread, '_sock')  # linters not angry
                peer_cert = sock.getpeercert(True)
                response = thread.make_request([
                    (':authority', '127.0.0.1'),
                    (':scheme', 'https'),
                    (':method', 'GET'),
                    (':path', '/ping/?foo=bar'),
                ])
                assert response.status_code == 200
        peer_cert = ssl.DER_cert_to_PEM_cert(peer_cert)
        assert peer_cert == cert_file_data
