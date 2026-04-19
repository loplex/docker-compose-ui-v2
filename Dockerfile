FROM alpine:3.23

RUN apk add --no-cache \
        'py3-flask' 'py3-werkzeug' 'py3-requests' \
        'py3-cryptography' 'py3-docker-py' 'py3-gitpython' \
        'docker-cli' 'docker-cli-compose'

COPY './docker_compose_ui' '/app/docker_compose_ui'
COPY './static' '/app/static'
COPY './main.py' '/app/main.py'
COPY './demo-projects' '/opt/docker-compose-projects'

WORKDIR '/opt/docker-compose-projects'

EXPOSE 5000

CMD [ "python3", "/app/main.py" ]
