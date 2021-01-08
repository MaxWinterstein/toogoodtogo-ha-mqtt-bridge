# inspired by https://medium.com/@harpalsahota/dockerizing-python-poetry-applications-1aa3acb76287
FROM python:3.7

RUN mkdir /app

COPY toogoodtogo_ha_mqtt_bridge/ /app
COPY pyproject.toml /app

WORKDIR /app

ENV PYTHONPATH=${PYTHONPATH}:${PWD}
RUN pip3 install poetry
RUN poetry config virtualenvs.create false
RUN poetry install --no-dev

CMD ["python", "main.py"]