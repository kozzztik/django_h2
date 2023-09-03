from django.dispatch import Signal

pre_request = Signal()
post_request = Signal()
request_exception = Signal()
