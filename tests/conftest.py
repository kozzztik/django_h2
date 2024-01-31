import os
import signal
import sys
import socket
from unittest import mock

import pytest


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
