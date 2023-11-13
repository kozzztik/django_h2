from django.http import JsonResponse


def ping(request):
    param = request.GET.get('param')
    return JsonResponse({'pong': param})
