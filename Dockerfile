FROM alpine:3.23

RUN apk add --no-cache \
        'py3-virtualenv' 'py3-cryptography' 'py3-docker-py' 'py3-gitpython' \
        'docker-cli' 'docker-cli-compose'

COPY './requirements.txt' '/app/requirements.txt'
RUN virtualenv --system-site-packages '/env' \
        && '/env/bin/pip' install --no-cache-dir 'cython<3.0.0' 'wheel' 'setuptools' \
        && '/env/bin/pip' install --no-cache-dir 'pyyaml==5.4.1' --no-build-isolation \
        && '/env/bin/pip' install --no-cache-dir -r '/app/requirements.txt'


COPY './scripts' '/app/scripts'
COPY './static' '/app/static'
COPY './main.py' '/app/main.py'
COPY './demo-projects' '/opt/docker-compose-projects'

EXPOSE 5000

ENTRYPOINT []
CMD [ "/env/bin/python", "/app/main.py" ]

WORKDIR '/opt/docker-compose-projects/'
