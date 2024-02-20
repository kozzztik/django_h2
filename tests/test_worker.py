import ssl
from importlib.resources import files
import logging
from unittest import mock

import pytest
from django import urls
from django.http import HttpResponse
from django.test import override_settings

from django_h2 import signals
from django_h2.gunicorn.app import DjangoGunicornApp
from django_h2.handler import AbstractHandler
from tests.utils import WorkerThread


def ping_view(request):
    return HttpResponse(str(dict(request.GET.items())).encode("utf-8"))


class UrlConf:  # pylint: disable=too-few-public-methods
    urlpatterns = [
        urls.re_path(r'^ping/$', ping_view),
    ]


@override_settings(ROOT_URLCONF=UrlConf)
def test_worker_init(server_sock):
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()

    with WorkerThread(server_sock, app) as thread:
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'http'),
            (':method', 'GET'),
            (':path', '/ping/?foo=bar')]
        )
    assert response.status_code == 200
    assert response.body == b"{'foo': 'bar'}"


@override_settings(ROOT_URLCONF=UrlConf)
def test_worker_ssl(server_sock):
    crt_file = str(files('django_h2').joinpath('default.crt'))
    with mock.patch('sys.argv', ['path', '--certfile', crt_file]):
        app = DjangoGunicornApp()

    with WorkerThread(server_sock, app) as thread:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        thread.connect_ssl(context)
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'https'),
            (':method', 'GET'),
            (':path', '/ping/?foo2=bar3')]
        )
    assert response.status_code == 200
    assert response.body == b"{'foo2': 'bar3'}"


@override_settings(ROOT_URLCONF=UrlConf)
def test_worker_ssl_min_version(server_sock):
    """
    RFC 7540 Section 9.2: Implementations of HTTP/2 MUST use TLS version 1.2
    """
    crt_file = str(files('django_h2').joinpath('default.crt'))
    with mock.patch('sys.argv', ['path', '--certfile', crt_file]):
        app = DjangoGunicornApp()

    with WorkerThread(server_sock, app) as thread:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with pytest.deprecated_call():
            context.maximum_version = ssl.TLSVersion.TLSv1_1
        with pytest.raises(ssl.SSLError) as e:
            thread.connect_ssl(context)
        assert 'no protocols available' in str(e)
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        thread.connect_ssl(context)
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'https'),
            (':method', 'GET'),
            (':path', '/ping/?foo4=bar5')]
        )
    assert response.status_code == 200
    assert response.body == b"{'foo4': 'bar5'}"


@override_settings(ROOT_URLCONF=UrlConf)
def test_config_post_request_exception(server_sock):
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()

    def post_request(sender, reqeust, env, resp):
        raise ValueError("foobar")
    app.cfg.set("post_request", post_request)
    with WorkerThread(server_sock, app) as thread:
        with mock.patch.object(app.logger, "exception") as logger_mock:
            response = thread.make_request([
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/ping/?foo=bar')]
            )
    assert response.status_code == 200
    assert response.body == b"{'foo': 'bar'}"
    assert logger_mock.called
    assert logger_mock.call_args[0][0] == "Exception in post_request hook %s"
    assert isinstance(logger_mock.call_args[0][1], ValueError)
    assert logger_mock.call_args[0][1].args == ('foobar',)


@override_settings(ROOT_URLCONF=UrlConf)
def test_keyboard_interrupt(server_sock):
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()

    class InterruptedThread(WorkerThread):
        async def stopping_task(self, loop):
            raise KeyboardInterrupt()

    with InterruptedThread(server_sock, app) as thread:
        thread.join(5)
        assert thread.worker.loop.is_closed()


@override_settings(ROOT_URLCONF=UrlConf)
def test_exceptions_logging(server_sock):
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()

    def failing_receiver(*args, **kwargs):
        raise ValueError("foobar")

    signals.request_finished.connect(failing_receiver)
    try:
        with mock.patch.object(app.logger, "exception") as logger_mock:
            with WorkerThread(server_sock, app) as thread:
                response = thread.make_request([
                    (':authority', '127.0.0.1'),
                    (':scheme', 'http'),
                    (':method', 'GET'),
                    (':path', '/ping/?foo=bar')]
                )
    finally:
        signals.request_finished.disconnect(failing_receiver)
    assert response.status_code == 200
    assert response.body == b"{'foo': 'bar'}"
    assert logger_mock.called
    assert isinstance(logger_mock.call_args[0][0], ValueError)
    assert logger_mock.call_args[0][0].args == ('foobar',)


@override_settings(ROOT_URLCONF=UrlConf)
def test_failed_loading_django_with_reload(server_sock):
    with mock.patch('sys.argv', ['path', '--reload']):
        app = DjangoGunicornApp()

    with mock.patch.object(app.logger, "exception") as logger_mock:
        with mock.patch("django.setup", side_effect=ValueError('foobar')):
            with WorkerThread(server_sock, app) as thread:
                response = thread.make_request([
                    (':authority', '127.0.0.1'),
                    (':scheme', 'http'),
                    (':method', 'GET'),
                    (':path', '/ping/?foo=bar')]
                )
    assert response.status_code == 500
    assert b'raise effect' in response.body
    assert logger_mock.called
    assert isinstance(logger_mock.call_args[0][0], ValueError)
    assert logger_mock.call_args[0][0].args == ('foobar',)


@override_settings(ROOT_URLCONF=UrlConf)
def test_failed_loading_django_no_reload(server_sock):
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()

    with mock.patch.object(logging, "exception") as logger_mock:
        with mock.patch("django.setup", side_effect=ValueError('foobar')):
            with WorkerThread(server_sock, app) as thread:
                thread.join(1)
    assert logger_mock.called
    assert isinstance(logger_mock.call_args[0][0], ValueError)
    assert logger_mock.call_args[0][0].args == ('foobar',)


@pytest.mark.filterwarnings(
    "ignore:StreamingHttpResponse must consume synchronous iterators")
@override_settings(STATICFILES_DIRS=[files('django_h2')], STATIC_URL='/static/')
@override_settings(ROOT_URLCONF=UrlConf)
def test_serving_static(server_sock):
    with mock.patch('sys.argv', ['path', '--serve_static']):
        app = DjangoGunicornApp()

    with WorkerThread(server_sock, app) as thread:
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'http'),
            (':method', 'GET'),
            (':path', '/static/default.crt')]
        )
        assert response.status_code == 200
        assert response['content-disposition'] == \
            'inline; filename="default.crt"'
        assert len(response.body) > 0
        # app still works
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'http'),
            (':method', 'GET'),
            (':path', '/ping/?foo5=bar6')]
        )
        assert response.status_code == 200
        assert response.body == b"{'foo5': 'bar6'}"


@override_settings(STATICFILES_DIRS=[files('django_h2')], STATIC_URL='/static/')
@override_settings(ROOT_URLCONF=UrlConf)
def test_not_serving_static_by_default(server_sock):
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()

    with WorkerThread(server_sock, app) as thread:
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'http'),
            (':method', 'GET'),
            (':path', '/static/default.crt')]
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_abstract_handler_not_implemented():
    handler = AbstractHandler()
    with pytest.raises(NotImplementedError):
        await handler.handle_request(None)
