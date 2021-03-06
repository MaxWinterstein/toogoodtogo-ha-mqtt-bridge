# inspired by https://medium.com/@harpalsahota/dockerizing-python-poetry-applications-1aa3acb76287
FROM python:3.7

RUN mkdir /app /data

COPY pyproject.toml toogoodtogo_ha_mqtt_bridge/ /app/

WORKDIR /app

ENV PYTHONPATH=${PYTHONPATH}:${PWD}
ENV CRYPTOGRAPHY_DONT_BUILD_RUST=1

RUN pip3 install poetry && \
    poetry config virtualenvs.create false && \
    poetry install --no-dev
ENV DYNACONF_DATA_DIR=/data
CMD ["python", "main.py"]
>>>>>>> Add cleanup function
