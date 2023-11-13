from django.urls import path
from performance.project import views

urlpatterns = [
    path(r'ping/', views.ping),
]
