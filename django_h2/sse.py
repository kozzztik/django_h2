from django_h2.request import H2Request
from django_h2.protocol import H2StreamingResponse


class SSEResponse(H2StreamingResponse):
    def __init__(self, request: H2Request, handler):
        self.handler = handler
        self.context = request.context
        super().__init__(
            status=200, content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',
            })

    def close(self):
        super().close()
        self.context.end_stream()

    async def send_event(self, name: str, data: str, event_id: str = None):
        event = [
            'event: ' + name.replace('\n', r'\n'),
            'data: ' + data.replace('\n', r'\n'),
        ]
        if event_id is not None:
            event.append('id: ' + str(event_id).replace('\n', r'\n'))
        event = '\n'.join(event).encode() + b'\n\n'
        await self.context.send_data(event, end_stream=False)
