import datetime

from performance.runner import core

COUNT = 5000


def main():
    test_results = []

    for env in core.ENVIRONMENTS:
        print('Testing env', env)
        url = f'/{env}/ping/'
        for i in range(COUNT):
            if i % 1000 == 0:
                print(i, '/', COUNT)
            response, resp_time = core.make_request('GET', url)
            if response.status != 200:
                raise Exception(response.reason)
            test_results.append({
                '@timestamp': datetime.datetime.utcnow().isoformat(),
                'env': env,
                'url': url,
                'time': resp_time * 1000
            })

    core.log_results(test_results)


if __name__ == '__main__':
    main()
