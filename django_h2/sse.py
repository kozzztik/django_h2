from collections import namedtuple
from typing import AsyncIterable
from django.http import StreamingHttpResponse


Event = namedtuple('Event', ('name', 'data', 'id'), defaults=(None,))


def format_event(event: Event) -> bytes:
    data = [
        'event: ' + event.name.replace('\n', r'\n'),
        'data: ' + event.data.replace('\n', r'\n'),
    ]
    if event.id is not None:
        data.append('id: ' + str(event.id).replace('\n', r'\n'))
    return '\n'.join(data).encode() + b'\n\n'


class SSEResponse(StreamingHttpResponse):
    is_async = True
    # override default getter/setter
    streaming_content: AsyncIterable[Event] = None

    # that is same as base class declaration
    # pylint: disable=keyword-arg-before-vararg
    def __init__(
            self, streaming_content: AsyncIterable[Event] = (), *args,
            content_type='text/event-stream',
            headers=None, **kwargs):
        sse_headers = {
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
        if headers:
            sse_headers.update(headers)
        super().__init__(
            *args,
            streaming_content=streaming_content,
            content_type=content_type,
            headers=sse_headers,
            **kwargs
        )

    async def __aiter__(self):
        async for part in self.streaming_content:
            yield format_event(part)
