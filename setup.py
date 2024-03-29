from setuptools import setup

__version__ = '0.1'

setup(
    name='django_h2',
    version=__version__,
    description='Django with fast HTTP2 support',
    long_description="""""",
    author='https://github.com/kozzztik',
    url='https://github.com/kozzztik/django_h2',
    keywords='email',
    packages=['django_h2'],
    package_data={
        'default.crt': ['django_h2/default.crt'],
    },
    include_package_data=True,
    license='https://github.com/kozzztik/django_h2/blob/master/LICENSE',
    classifiers=[
        'License :: OSI Approved',
        'Intended Audience :: Developers',
        'Programming Language :: Python :: 3.11',
        ],
    )