"""
bridge to docker compose v2 (CLI) + docker SDK v7
"""

import logging
import subprocess
import json
from os.path import normpath, join, exists

import docker

_docker_client = None


def client():
    """docker client (singleton)"""
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


def _run_compose(path, *args, check=True):
    """Run `docker compose` CLI in the given project directory."""
    cmd = ['docker', 'compose'] + list(args)
    logging.info('Running: %s in %s', ' '.join(cmd), path)
    return subprocess.run(cmd, cwd=path, capture_output=True, text=True, check=check)


def info():
    """docker info"""
    docker_info = client().info()
    try:
        result = subprocess.run(['docker', 'compose', 'version', '--short'],
                                capture_output=True, text=True)
        compose_version = result.stdout.strip()
    except Exception:
        compose_version = 'unknown'
    return dict(compose=compose_version,
                info=docker_info['ServerVersion'],
                name=docker_info['Name'])


def containers():
    """active containers (list of dicts for API compatibility)"""
    raw = client().api.containers()
    return raw


def get_yml_path(path):
    """get path of docker-compose.yml file"""
    for name in ('docker-compose.yml', 'docker-compose.yaml',
                 'compose.yml', 'compose.yaml'):
        candidate = join(path, name)
        if exists(candidate):
            return candidate
    raise FileNotFoundError(f'No docker-compose file found in {path}')


class _ProjectProxy:
    """
    Wraps a docker compose project path and exposes the API
    previously provided by compose.project.Project.
    """

    def __init__(self, path):
        self.path = normpath(path)
        self.name = self.path.rstrip('/').split('/')[-1]
        self.client = client().api

    def containers(self, stopped=False):
        filters = {'label': f'com.docker.compose.project={self.name}'}
        raw = client().containers.list(all=stopped, filters=filters)
        return [_ContainerProxy(c) for c in raw]

    def up(self, service_names=None, do_build=None):
        args = ['up', '-d']
        if do_build == 'force':
            args.append('--build')
        if service_names:
            args += list(service_names)
        _run_compose(self.path, *args)
        return self.containers(stopped=False)

    def down(self, image_type=None, timeout=None):
        _run_compose(self.path, 'down')

    def kill(self):
        _run_compose(self.path, 'kill')

    def start(self):
        _run_compose(self.path, 'start')

    def stop(self):
        _run_compose(self.path, 'stop')

    def restart(self):
        _run_compose(self.path, 'restart')

    def pull(self):
        _run_compose(self.path, 'pull')

    def build(self, no_cache=None, pull=None):
        args = ['build']
        if no_cache:
            args.append('--no-cache')
        if pull:
            args.append('--pull')
        _run_compose(self.path, *args)

    def create(self):
        _run_compose(self.path, 'create')

    def remove_stopped(self):
        _run_compose(self.path, 'rm', '-f')

    def get_service(self, service_name):
        return _ServiceProxy(self, service_name)


class _ContainerProxy:
    """
    Wraps docker.models.containers.Container to expose the API
    previously provided by compose.container.Container.
    """

    def __init__(self, container):
        self._c = container
        self._c.reload()

    @property
    def id(self):
        return self._c.id

    @property
    def short_id(self):
        return self._c.short_id

    @property
    def name(self):
        return self._c.name

    @property
    def name_without_project(self):
        name = self._c.name
        labels = self._c.labels or {}
        project = labels.get('com.docker.compose.project', '')
        if project and name.startswith(project + '_'):
            return name[len(project) + 1:]
        return name

    @property
    def number(self):
        labels = self._c.labels or {}
        try:
            return int(labels.get('com.docker.compose.container-number', 0))
        except ValueError:
            return 0

    @property
    def human_readable_command(self):
        cmd = self._c.attrs.get('Config', {}).get('Cmd') or []
        return ' '.join(cmd) if isinstance(cmd, list) else str(cmd)

    @property
    def human_readable_state(self):
        return self._c.status

    @property
    def is_running(self):
        return self._c.status == 'running'

    @property
    def labels(self):
        return self._c.labels or {}

    @property
    def ports(self):
        return self._c.ports or {}

    @property
    def image(self):
        tags = self._c.image.tags if self._c.image else []
        return tags[0] if tags else ''

    @property
    def image_config(self):
        img = self._c.image
        return {'RepoTags': img.tags if img else []}

    @property
    def environment(self):
        env_list = self._c.attrs.get('Config', {}).get('Env') or []
        result = {}
        for item in env_list:
            if '=' in item:
                k, v = item.split('=', 1)
                result[k] = v
        return result

    @property
    def log_config(self):
        return self._c.attrs.get('HostConfig', {}).get('LogConfig', {})

    def get(self, key):
        """Dot-notation access into container inspect data."""
        parts = key.split('.')
        val = self._c.attrs
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p)
            else:
                return None
        return val

    def logs(self, timestamps=False, tail='all'):
        return self._c.logs(timestamps=timestamps, tail=tail)

    def start(self):
        self._c.start()


class _ServiceProxy:
    """Minimal proxy for a single compose service."""

    def __init__(self, project: _ProjectProxy, service_name: str):
        self._project = project
        self._name = service_name

    @property
    def options(self):
        try:
            result = _run_compose(self._project.path, 'config', '--format', 'json')
            cfg = json.loads(result.stdout)
            svc = cfg.get('services', {}).get(self._name, {})
            return {'command': svc.get('command')}
        except Exception:
            return {}

    def create_container(self, one_off=False, command=None):
        args = ['run', '-d']
        if one_off:
            args.append('--rm')
        args.append(self._name)
        if command:
            if isinstance(command, list):
                args += command
            else:
                args += command.split()
        result = _run_compose(self._project.path, *args)
        container_id = result.stdout.strip().split('\n')[-1].strip()
        raw = client().containers.get(container_id)
        return _ContainerProxy(raw)

    def scale(self, desired_num=1):
        _run_compose(self._project.path, 'up', '-d',
                     '--scale', f'{self._name}={desired_num}',
                     self._name)


# ------------------------------------------------------------------ #
# Public API                                                          #
# ------------------------------------------------------------------ #

def get_project(path):
    """Return a _ProjectProxy for the given path."""
    logging.debug('get project ' + path)
    return _ProjectProxy(path)


def ps_(project):
    """containers status"""
    logging.info('ps ' + project.name)
    running_containers = project.containers(stopped=True)
    return [{
        'name': c.name,
        'name_without_project': c.name_without_project,
        'command': c.human_readable_command,
        'state': c.human_readable_state,
        'labels': c.labels,
        'ports': c.ports,
        'volumes': get_volumes(c),
        'is_running': c.is_running,
    } for c in running_containers]


def get_volumes(container):
    """retrieve container volumes details"""
    mounts = container.get('Mounts') or []
    return [dict(source=m.get('Source', ''), destination=m.get('Destination', ''))
            for m in mounts]


def get_container_from_id(docker_api_client, container_id):
    """return a _ContainerProxy for the given container id"""
    raw = client().containers.get(container_id)
    return _ContainerProxy(raw)


def project_config(path):
    """
    docker compose config – returns a namedtuple compatible with
    the _replace() call in main.py
    """
    result = _run_compose(path, 'config', '--format', 'json', check=False)
    cfg = {}
    if result.returncode == 0:
        try:
            cfg = json.loads(result.stdout)
        except Exception:
            pass

    version = cfg.get('version', '')

    from collections import namedtuple
    ConfigResult = namedtuple('ConfigResult',
                              ['version', 'config_version', 'services', 'volumes', 'networks'])
    return ConfigResult(
        version=version,
        config_version=version,
        services=cfg.get('services', {}),
        volumes=cfg.get('volumes', {}),
        networks=cfg.get('networks', {}),
    )