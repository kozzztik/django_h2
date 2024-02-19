from django.dispatch import Signal

server_started = Signal()
stream_started = Signal()
pre_request = Signal()
post_request = Signal()
request_exception = Signal()
