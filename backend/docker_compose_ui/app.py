"""
Docker Compose UI – Flask application factory
"""

from json import loads
import logging
import os
import pathlib
import threading
import traceback
from shutil import rmtree
import docker
import docker.errors
import requests
from flask import Flask, jsonify, request, abort, send_file
from werkzeug.exceptions import HTTPException

from .git_repo import git_repo, git_pull, GIT_YML_PATH
from .bridge import get_project, containers, info, get_container_from_id, get_yml_path, project_config, ps_, warmup
from .requires_auth import requires_auth, authentication_enabled, set_authentication, disable_authentication
from .find_files import find_yml_files, get_readme_file, get_logo_file
from .manage_project import manage

API_V1 = '/api/v1/'
YML_PATH = os.getenv('DOCKER_COMPOSE_UI_YML_PATH') or '.'
COMPOSE_REGISTRY = os.getenv('DOCKER_COMPOSE_REGISTRY')
STATIC_URL_PATH = '/' + (os.getenv('DOCKER_COMPOSE_UI_PREFIX') or '')
_PACKAGE_DIR = pathlib.Path(__file__).parent

logging.basicConfig(level=logging.INFO)

# Module-level project registry
projects: dict = {}


def _load_projects() -> None:
    """Load project definitions (docker-compose.yml files)."""
    global projects
    if git_repo:
        git_pull()
        projects = find_yml_files(GIT_YML_PATH)
    else:
        projects = find_yml_files(YML_PATH)
    logging.info(projects)


def _warmup_async() -> None:
    """Warm up Docker client in a background thread (used at startup)."""
    threading.Thread(target=warmup, daemon=True).start()


def _get_project_with_name(name: str):
    """Return a docker compose project object for the given project name."""
    return get_project(projects[name])


def create_app(static_path: str | None = None) -> Flask:
    """Application factory.

    Args:
        static_path: Path to the ``static`` directory.
                     Falls back to DOCKER_COMPOSE_UI_STATIC_PATH env var,
                     then to ``static/`` relative to CWD.
    """
    resolved = pathlib.Path(
        static_path
        or os.getenv('DOCKER_COMPOSE_UI_STATIC_PATH')
        or (pathlib.Path.cwd() / "static")
    )
    if not resolved.is_dir():
        raise RuntimeError(
            f"Static files directory not found: '{resolved}'. "
            "Pass a path to create_app(), or set the "
            "DOCKER_COMPOSE_UI_STATIC_PATH environment variable."
        )

    def _prefix_route(route_function, prefix='', mask='{0}{1}'):
        """Define a new route function with a prefix.
        Pulled from https://stackoverflow.com/a/37878456 (user 7heo.tk).
        """
        def newroute(route, *args, **kwargs):
            return route_function(mask.format(prefix, route), *args, **kwargs)
        return newroute

    app = Flask(__name__, static_url_path=STATIC_URL_PATH, static_folder=str(resolved))
    app.route = _prefix_route(app.route, prefix=STATIC_URL_PATH)

    _warmup_async()

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.route(API_V1 + "projects", methods=['GET'])
    def list_projects():
        """List docker compose projects."""
        _load_projects()
        active = [
            container['Labels']['com.docker.compose.project']
            if 'com.docker.compose.project' in container['Labels']
            else []
            for container in containers()
        ]
        return jsonify(projects=projects, active=active)

    @app.route(API_V1 + "remove/<name>", methods=['DELETE'])
    @requires_auth
    def rm_(name):
        """Remove previous cached containers (docker-compose rm -f)."""
        _get_project_with_name(name).remove_stopped()
        return jsonify(command='rm')

    @app.route(API_V1 + "projects/<name>", methods=['GET'])
    def project_containers(name):
        """Get project details."""
        return jsonify(containers=ps_(_get_project_with_name(name)))

    @app.route(API_V1 + "projects/<project>/<service_id>", methods=['POST'])
    @requires_auth
    def run_service(project, service_id):
        """docker-compose run service."""
        json = loads(request.data)
        service = _get_project_with_name(project).get_service(service_id)
        command = json["command"] if 'command' in json else service.options.get('command')
        container = service.create_container(one_off=True, command=command)
        container.start()
        return jsonify(
            command='run %s/%s' % (project, service_id),
            name=container.name,
            id=container.id,
        )

    @app.route(API_V1 + "projects/yml/<name>", methods=['GET'])
    def project_yml(name):
        """Get yml content."""
        folder_path = projects[name]
        path = get_yml_path(folder_path)
        config = project_config(folder_path)
        with open(path) as data_file:
            env = None
            if os.path.isfile(folder_path + '/.env'):
                with open(folder_path + '/.env') as env_file:
                    env = env_file.read()
            return jsonify(
                yml=data_file.read(),
                env=env,
                config=dict(
                    version=str(config.version),
                    config_version=str(config.config_version),
                    services=config.services,
                    volumes=config.volumes,
                    networks=config.networks,
                ),
            )

    @app.route(API_V1 + "projects/readme/<name>", methods=['GET'])
    def get_project_readme(name):
        """Get README.md if available."""
        readme_path = get_readme_file(projects[name])
        if readme_path is None:
            return jsonify(readme=None)
        with open(readme_path, encoding='utf-8') as f:
            return jsonify(readme=f.read())

    @app.route(API_V1 + "projects/logo/<name>", methods=['GET'])
    def get_project_logo(name):
        """Get logo.png if available."""
        logo = get_logo_file(projects[name])
        if logo is None:
            abort(404)
        return send_file(logo, mimetype='image/png')

    @app.route(API_V1 + "projects/<name>/<container_id>", methods=['GET'])
    def project_container(name, container_id):
        """Get container details."""
        _get_project_with_name(name)  # validates project exists
        container = get_container_from_id(container_id)
        return jsonify(
            id=container.id,
            short_id=container.short_id,
            human_readable_command=container.human_readable_command,
            name=container.name,
            name_without_project=container.name_without_project,
            number=container.number,
            ports=container.ports,
            ip=container.get('NetworkSettings.IPAddress'),
            labels=container.labels,
            log_config=container.log_config,
            image=container.image,
            environment=container.environment,
            started_at=container.get('State.StartedAt'),
            repo_tags=container.image_config['RepoTags'],
        )

    @app.route(API_V1 + "projects/<name>", methods=['DELETE'])
    @requires_auth
    def kill(name):
        """docker-compose kill."""
        _get_project_with_name(name).kill()
        return jsonify(command='kill')

    @app.route(API_V1 + "projects", methods=['PUT'])
    @requires_auth
    def pull():
        """docker-compose pull."""
        name = loads(request.data)["id"]
        _get_project_with_name(name).pull()
        return jsonify(command='pull')

    @app.route(API_V1 + "services", methods=['PUT'])
    @requires_auth
    def scale():
        """docker-compose scale."""
        req = loads(request.data)
        project = _get_project_with_name(req['project'])
        project.get_service(req['service']).scale(desired_num=int(req['num']))
        return jsonify(command='scale')

    @app.route(API_V1 + "projects", methods=['POST'])
    @requires_auth
    def up_():
        """docker-compose up."""
        req = loads(request.data)
        name = req["id"]
        service_names = req.get('service_names', None)
        do_build = 'force' if req.get('do_build', False) else 'none'
        container_list = _get_project_with_name(name).up(
            service_names=service_names,
            do_build=do_build,
        )
        return jsonify(command='up', containers=[c.name for c in container_list])

    @app.route(API_V1 + "build", methods=['POST'])
    @requires_auth
    def build():
        """docker-compose build."""
        json = loads(request.data)
        name = json["id"]
        kwargs = dict(
            no_cache=json.get("no_cache"),
            pull=json.get("pull"),
        )
        _get_project_with_name(name).build(**kwargs)
        return jsonify(command='build')

    @app.route(API_V1 + "create-project", methods=['POST'])
    @app.route(API_V1 + "create", methods=['POST'])
    @requires_auth
    def create_project():
        """Create a new project."""
        data = loads(request.data)
        file_path = manage(YML_PATH + '/' + data["name"], data["yml"], False)
        if data.get("env"):
            with open(YML_PATH + '/' + data["name"] + "/.env", "w") as env_file:
                env_file.write(data["env"])
        _load_projects()
        return jsonify(path=file_path)

    @app.route(API_V1 + "update-project", methods=['PUT'])
    @requires_auth
    def update_project():
        """Update an existing project."""
        data = loads(request.data)
        file_path = manage(YML_PATH + '/' + data["name"], data["yml"], True)
        if data.get("env"):
            with open(YML_PATH + '/' + data["name"] + "/.env", "w") as env_file:
                env_file.write(data["env"])
        return jsonify(path=file_path)

    @app.route(API_V1 + "remove-project/<name>", methods=['DELETE'])
    @requires_auth
    def remove_project(name):
        """Remove a project directory."""
        directory = YML_PATH + '/' + name
        rmtree(directory)
        _load_projects()
        return jsonify(path=directory)

    @app.route(API_V1 + "search", methods=['POST'])
    def search():
        """Search for a project on a docker-compose registry."""
        if not COMPOSE_REGISTRY:
            return 'DOCKER_COMPOSE_REGISTRY is not configured', 503
        query = loads(request.data)['query']
        response = requests.get(
            COMPOSE_REGISTRY + '/api/v1/search',
            params={'query': query},
            headers={'x-key': 'default'},
        )
        result = jsonify(response.json())
        if response.status_code != 200:
            result.status_code = response.status_code
        return result

    @app.route(API_V1 + "yml", methods=['POST'])
    def yml():
        """Get yml content from a docker-compose registry."""
        if not COMPOSE_REGISTRY:
            return 'DOCKER_COMPOSE_REGISTRY is not configured', 503
        item_id = loads(request.data)['id']
        response = requests.get(
            COMPOSE_REGISTRY + '/api/v1/yml',
            params={'id': item_id},
            headers={'x-key': 'default'},
        )
        return jsonify(response.json())

    @app.route(API_V1 + "_create", methods=['POST'])
    @requires_auth
    def create():
        """docker-compose create."""
        name = loads(request.data)["id"]
        _get_project_with_name(name).create()
        return jsonify(command='create')

    @app.route(API_V1 + "start", methods=['POST'])
    @requires_auth
    def start():
        """docker-compose start."""
        name = loads(request.data)["id"]
        _get_project_with_name(name).start()
        return jsonify(command='start')

    @app.route(API_V1 + "stop", methods=['POST'])
    @requires_auth
    def stop():
        """docker-compose stop."""
        name = loads(request.data)["id"]
        _get_project_with_name(name).stop()
        return jsonify(command='stop')

    @app.route(API_V1 + "down", methods=['POST'])
    @requires_auth
    def down():
        """docker-compose down."""
        name = loads(request.data)["id"]
        _get_project_with_name(name).down()
        return jsonify(command='down')

    @app.route(API_V1 + "restart", methods=['POST'])
    @requires_auth
    def restart():
        """docker-compose restart."""
        name = loads(request.data)["id"]
        _get_project_with_name(name).restart()
        return jsonify(command='restart')

    @app.route(API_V1 + "logs/<name>", defaults={'limit': "all"}, methods=['GET'])
    @app.route(API_V1 + "logs/<name>/<int:limit>", methods=['GET'])
    def logs(name, limit):
        """docker-compose logs."""
        lines = {
            k.name: k.logs(timestamps=True, tail=limit).decode().split('\n')
            for k in _get_project_with_name(name).containers(stopped=True)
        }
        return jsonify(logs=lines)

    @app.route(API_V1 + "logs/<name>/<container_id>", defaults={'limit': "all"}, methods=['GET'])
    @app.route(API_V1 + "logs/<name>/<container_id>/<int:limit>", methods=['GET'])
    def container_logs(name, container_id, limit):
        """docker-compose logs of a specific container."""
        _get_project_with_name(name)  # validates project exists
        container = get_container_from_id(container_id)
        lines = container.logs(timestamps=True, tail=limit).decode().split('\n')
        return jsonify(logs=lines)

    @app.route(API_V1 + "host", methods=['GET'])
    def host():
        """Docker host info."""
        return jsonify(
            host=os.getenv('DOCKER_HOST'),
            workdir=os.getcwd() if YML_PATH == '.' else YML_PATH,
        )

    @app.route(API_V1 + "compose-registry", methods=['GET'])
    def compose_registry():
        """Docker compose registry URL."""
        return jsonify(url=COMPOSE_REGISTRY)

    @app.route(API_V1 + "web_console_pattern", methods=['GET'])
    def get_web_console_pattern():
        """Forward WEB_CONSOLE_PATTERN env var to the SPA."""
        return jsonify(web_console_pattern=os.getenv('WEB_CONSOLE_PATTERN'))

    @app.route(API_V1 + "health", methods=['GET'])
    def health():
        """Docker health."""
        return jsonify(info())

    @app.route(API_V1 + "host", methods=['POST'])
    @requires_auth
    def set_host():
        """Set docker host."""
        new_host = loads(request.data)["id"]
        if new_host is None:
            os.environ.pop('DOCKER_HOST', None)
            return jsonify()
        os.environ['DOCKER_HOST'] = new_host
        return jsonify(host=new_host)

    @app.route(API_V1 + "authentication", methods=['GET'])
    def authentication():
        """Check if basic authentication is enabled."""
        return jsonify(enabled=authentication_enabled())

    @app.route(API_V1 + "authentication", methods=['DELETE'])
    @requires_auth
    def disable_basic_authentication():
        """Disable basic authentication."""
        disable_authentication()
        return jsonify(enabled=False)

    @app.route(API_V1 + "authentication", methods=['POST'])
    @requires_auth
    def enable_basic_authentication():
        """Set up basic authentication."""
        data = loads(request.data)
        set_authentication(data["username"], data["password"])
        return jsonify(enabled=True)

    @app.route("/")
    def index():
        """Serve index.html."""
        return app.send_static_file('index.html')

    # ------------------------------------------------------------------
    # Error handlers
    # ------------------------------------------------------------------

    @app.errorhandler(requests.exceptions.ConnectionError)
    def handle_connection_error(err):
        return 'docker host not found: ' + str(err), 500

    @app.errorhandler(docker.errors.DockerException)
    def handle_docker_error(err):
        return 'docker exception: ' + str(err), 500

    @app.errorhandler(Exception)
    def handle_generic_error(err):
        if isinstance(err, HTTPException):
            return err
        traceback.print_exc()
        return 'error: ' + str(err), 500

    return app
