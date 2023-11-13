import http.client
import json
import time

ELASTIC_HOST = '127.0.0.1:9200'
INDEX = 'logging'
PACK_SIZE = 100
HOST = '127.0.0.1'

ENVIRONMENTS = ['django_h2', 'django_asgi', 'django_wsgi']


def _bulk_index(data):
    lines = []
    for item in data:
        lines.append(json.dumps({'index': {}}))
        lines.append(json.dumps(item))
    lines.append('')
    conn = http.client.HTTPConnection(ELASTIC_HOST)
    try:
        conn.request(
            'POST', f'/{INDEX}/_bulk',
            headers={'Content-Type': 'application/json'},
            body='\n'.join(lines).encode('utf-8'))
        response = conn.getresponse()
        print('Logging:', response.status, response.reason, len(data))
    finally:
        conn.close()


def make_request(*args, **kwargs) -> (http.client.HTTPResponse, float):
    retry = 0
    start = time.time()
    conn = http.client.HTTPConnection(HOST)
    while retry < 3:
        try:
            conn.request(*args, **kwargs)
            response = conn.getresponse()
            resp_time = time.time() - start
            if response.status == 502:
                print('Bad gateway, sleep 1s')
                time.sleep(1)
                retry += 1
                continue
            return response, resp_time
        finally:
            conn.close()


def log_results(data):
    while data:
        _bulk_index(data[:PACK_SIZE])
        data = data[PACK_SIZE:]
