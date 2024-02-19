from django.dispatch import Signal

server_started = Signal()
stream_started = Signal()
request_finished = Signal()
request_exception = Signal()
