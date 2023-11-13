FROM python:3.11
EXPOSE 7000
ENV PYTHONUNBUFFERED 1
ENV DJANGO_SETTINGS_MODULE performance.settings
RUN mkdir /opt/performance
WORKDIR /opt/

RUN pip install --upgrade pip
ADD requirements.txt /opt/requirements.txt
RUN pip install -r requirements.txt

ADD project /opt/performance/project
ADD settings.py /opt/performance/settings.py
ADD server_key.key /opt/performance/server_key.key
ADD server_key.crt /opt/performance/server_key.crt

CMD [ "gunicorn" ]
