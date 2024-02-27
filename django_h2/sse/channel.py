import json

from django.core.serializers.json import DjangoJSONEncoder

from django_h2.sse.response import Event


class Channel:
    name_template = ''
    history = 0
    serializer = DjangoJSONEncoder()
    deserializer = json.JSONDecoder()

    def __init__(self, *args, **kwargs):
        self.name = self.get_name(*args, **kwargs)

    def get_name(self, *args, **kwargs):
        return self.name_template.format(*args, **kwargs)

    def get_history_list_key(self):
        return f'{self.name}_history'

    def get_history_counter_key(self):
        return f'{self.name}_counter'

    def __repr__(self):
        return f"{self.__class__.__name__}: {self.name}"

    def deserialize(self, message, **context) -> Event:
        return Event(
            **self.deserializer.decode(message["data"].decode("utf-8"))
        )
