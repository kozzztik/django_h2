FROM python:3.11
ENV PYTHONUNBUFFERED 1
ENV PYTHONASYNCIODEBUG 1
RUN apt-get update
RUN apt-get -y install python3-openssl
# for better caching
## install requirements, so they can be cached by Docker
RUN pip install django==5.0.2 h2==4.1.0 gunicorn==21.2.0 pytest==7.2.1 \
    pytest-cov==4.0.0 pylint==2.16.1 billiard==4.1.0 pycodestyle==2.8.0 \
    flake8==4.0.1 pytest-flake8==1.1.1 coverage==6.5.0 coveralls==3.3.1 \
    pytest-asyncio==0.20.3 pyopenssl==24.0.0

ADD scripts/run_tests.sh /opt/run_tests.sh
RUN chmod +x /opt/run_tests.sh
CMD /opt/run_tests.sh
ADD django_h2 /opt/django_h2
ADD tests /opt/tests
ADD .pylintrc /opt/.pylintrc
ADD .git /opt/.git
ADD pytest.ini /opt/pytest.ini
ADD requirements.txt /opt/requirements.txt
ADD dev_requirements.txt /opt/dev_requirements.txt
WORKDIR /opt
RUN pip install -r requirements.txt
RUN pip install -r dev_requirements.txt
