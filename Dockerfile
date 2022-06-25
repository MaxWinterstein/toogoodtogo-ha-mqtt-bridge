# inspired by https://medium.com/@harpalsahota/dockerizing-python-poetry-applications-1aa3acb76287
FROM python:3.7

RUN mkdir /app /data
WORKDIR /app

ENV PYTHONPATH=${PYTHONPATH}:${PWD}
ENV CRYPTOGRAPHY_DONT_BUILD_RUST=1
RUN pip install cryptography==3.3.2

# copy requirements first to create better cache layers
COPY requirements.txt /app/
RUN pip install -r requirements.txt

COPY . /app/
RUN python setup.py install

ENV DYNACONF_DATA_DIR=/data
CMD ["python", "toogoodtogo_ha_mqtt_bridge/main.py"]
