#!/bin/bash
set -ex
echo "Running pylint"
python3 -m pylint django_h2 tests
python3 -m pytest django_h2 tests --cov=django_h2 --cov-report term-missing --cov-report term:skip-covered
if [ -n "$COVERALLS_REPO_TOKEN" ]; then
    coveralls
fi
