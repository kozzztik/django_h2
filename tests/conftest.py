import os
import signal
import sys
import socket
from unittest import mock

import pytest

from django_h2.signals import post_request
from tests import gunicorn_conf


@pytest.fixture(name="server_sock")
def server_sock_fixture():
    sock_server = socket.socket()
    sock_server.bind(('127.0.0.1', 0))
    yield sock_server
    sock_server.close()


@pytest.fixture(autouse=True)
def gunicorn_default_config():
    with mock.patch(
            'gunicorn.app.base.get_default_config_file',
            return_value=gunicorn_conf.__file__):
        yield


@pytest.fixture(name="post_request_signal")
def post_request_signal_fixture():
    signal_calls = []

    def receiver(**kwargs):
        signal_calls.append(kwargs)
    post_request.connect(receiver)
    try:
        yield signal_calls
    finally:
        post_request.disconnect(receiver)


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session):
    # for testing under windows
    sys.modules['fcntl'] = mock.MagicMock()
    sys.modules['pwd'] = mock.MagicMock()
    sys.modules['grp'] = mock.MagicMock()

    signal.SIGHUP = mock.MagicMock()
    signal.SIGQUIT = mock.MagicMock()
    signal.SIGUSR1 = mock.MagicMock()
    signal.SIGUSR2 = mock.MagicMock()
    signal.SIGWINCH = mock.MagicMock()
    signal.SIGCHLD = mock.MagicMock()
    signal.SIGTTIN = mock.MagicMock()
    signal.SIGTTOU = mock.MagicMock()

    socket.AF_UNIX = mock.MagicMock()

    os.geteuid = mock.MagicMock()
    os.getegid = mock.MagicMock()
    os.chown = mock.MagicMock()
    # pylint: disable=import-outside-toplevel
    from gunicorn.workers import workertmp
    workertmp.IS_CYGWIN = True
