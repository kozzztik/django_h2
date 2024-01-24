from django_h2.request import H2Request
from django_h2.protocol import H2StreamingResponse


class SSEResponse(H2StreamingResponse):
    def __init__(self, request: H2Request, handler):
        self.handler = handler
        self.protocol = request.h2_protocol
        self.stream_id = request.h2_stream_id
        super().__init__(
            status=200, content_type='text/event-stream',
            headers={
                'Transfer-Encoding': 'chunked',
                'Connection': 'Transfer-Encoding'
            })

    def close(self):
        super().close()
        self.protocol.end_stream(self.stream_id)

    async def send_event(self, name: str, data: str, event_id: str = None):
        event = [
            'event: ' + name.replace('\n', r'\n'),
            'data: ' + data.replace('\n', r'\n'),
        ]
        if event_id is not None:
            event.append('id: ' + str(event_id).replace('\n', r'\n'))
        event = '\n'.join(event).encode() + b'\n\n'
        await self.protocol.send_data(event, self.stream_id, end_stream=False)
