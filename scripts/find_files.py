"""
find docker-compose.yml files
"""

import os

COMPOSE_FILENAMES = (
    'compose.yaml',
    'compose.yml',
    'docker-compose.yaml',
    'docker-compose.yml',
)

def find_yml_files(path):
    """
    find docker-compose files in path
    """

    matches = {}
    for root, dirs, filenames in os.walk(path, followlinks=True):
        filenames_lower = {f.lower() for f in filenames}
        if filenames_lower.intersection(COMPOSE_FILENAMES):
            key = root.split('/')[-1]
            matches[key] = os.path.join(os.getcwd(), str(root))

    return matches


def get_readme_file(path):
    """
    find case-insensitive readme.md in path and return the full file path
    """

    for file in os.listdir(path):
        if file.lower() == "readme.md" and os.path.isfile(os.path.join(path, file)):
            return os.path.join(path, file)

    return None

def get_logo_file(path):
    """
    find case-insensitive logo.png in path and return the full file path
    """

    for file in os.listdir(path):
        if file.lower() == "logo.png" and os.path.isfile(os.path.join(path, file)):
            return os.path.join(path, file)

    return None
