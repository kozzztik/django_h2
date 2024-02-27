import asyncio
import logging

import redis
from redis import asyncio as aioredis

from django_h2.request import H2Request
from django_h2.sse.response import Event, SSEResponse
from django_h2.sse.channel import Channel


logger = logging.getLogger(__name__)


class RedisServer:
    _task: asyncio.Task | None = None

    def __init__(self, host, port, db, key_prefix='', **kwargs):
        self.key_prefix = key_prefix
        self.client = redis.Redis(
            host=host, port=port, db=db, **kwargs)
        self.aclient = aioredis.client.Redis(
            host=host, port=port, db=db, **kwargs)
        self._channels = {}
        self._pubsub = self.aclient.pubsub()

    def publish_message(self, channel: Channel, event: Event) -> int:
        # TODO: publish to history
        return self.client.publish(
            f'{self.key_prefix}{channel.name}',
            channel.serializer.encode(event._asdict())
        )

    def close(self):
        # TODO: close on server stop?
        if self._task:
            self._task.cancel()
            self._task = None
        if self._pubsub:
            asyncio.create_task(self._pubsub.aclose())
            self._pubsub = None

    def __del__(self):
        self.close()  # todo: create task on delete?

    @staticmethod
    def _pubsub_exc_handler(e, *args):
        logger.exception(e)

    async def _get_message(self, message):
        channel_name = message['channel'].decode("utf-8")
        for listener in self._channels.get(channel_name, []):
            listener.put_nowait(message)

    async def subscribe(self, channel: Channel) -> asyncio.Queue:
        queue = asyncio.Queue()
        if channel.name not in self._channels:
            self._channels[channel.name] = []
            # TODO: if failed?
            await self._pubsub.subscribe(
                **{f'{self.key_prefix}{channel.name}': self._get_message}
            )
        self._channels[channel.name].append(queue)
        if not self._task:
            self._task = asyncio.create_task(
                self._pubsub.run(
                    exception_handler=self._pubsub_exc_handler,
                    poll_timeout=30
                )
            )
        return queue

    async def unsubscribe(self, channel: Channel, queue: asyncio.Queue):
        if channel.name in self._channels:
            listeners = self._channels[channel.name]
            if queue in listeners:
                listeners.remove(queue)
            if not listeners:
                del self._channels[channel.name]
        # TODO: close if no self._channels?

    async def events_source(
            self, request: H2Request, channel: Channel, **context):
        queue = await self.subscribe(channel)
        # TODO load history?
        try:
            while True:
                message = await queue.get()
                event = channel.deserialize(message, request=request, **context)
                if event:
                    yield event
        except Exception as e:
            print(e)  # TODO?
        finally:
            await self.unsubscribe(channel, queue)

    def as_view(self, channel: Channel):
        def view(request: H2Request, **kwargs):
            return SSEResponse(
                self.events_source(request, channel, **kwargs)
            )
        return view
