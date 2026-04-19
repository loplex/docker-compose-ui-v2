"""
Entry point for Docker Compose UI.
Run directly:
    python main.py
Or via the installed script:
    docker-compose-ui
"""
import pathlib
from docker_compose_ui.app import create_app

_HERE = pathlib.Path(__file__).parent
app = create_app(static_path=str(_HERE / "static"))

if __name__ == "__main__":
    app.run(host='0.0.0.0', threaded=True)
