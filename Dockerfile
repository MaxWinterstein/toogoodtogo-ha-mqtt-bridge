FROM python:3.12

# renovate: datasource=github-releases depName=uv packageName=astral-sh/uv
ENV UV_VERSION="0.7.2"
RUN pip install uv==$UV_VERSION

# Change the working directory to the `app` directory
WORKDIR /app

# Copy the lockfile and `pyproject.toml` into the image
ADD uv.lock /app/uv.lock
ADD pyproject.toml /app/pyproject.toml

# Install dependencies
RUN uv sync --frozen --no-install-project --no-dev

# Copy the project into the image
ADD . /app

# Sync the project (with mounted .git for setuptools_scm to work)
RUN --mount=source=.git,target=.git,type=bind \
  uv sync --frozen --no-dev

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# Poor mans test if at least the imports work
RUN python toogoodtogo_ha_mqtt_bridge/main.py --version

# Run
ENV DYNACONF_DATA_DIR=/data
CMD ["python", "toogoodtogo_ha_mqtt_bridge/main.py"]
