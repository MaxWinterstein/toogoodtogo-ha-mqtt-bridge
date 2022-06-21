from setuptools import setup

setup(
    name="toogoodtogo-ha-mqtt-bridge",
    version="2.2.3",
    description="Something different",
    author="Max Winterstein",
    author_email="github@winterstein.mx",
    packages=["toogoodtogo_ha_mqtt_bridge"],  # would be the same as name
    install_requires=[
        "paho-mqtt",
        "dynaconf",
        "tgtg",
        "coloredlogs",
        "tenacity",
        "arrow",
        "croniter",
        "google-play-scraper",
        "random_user_agent",
        "packaging",
        "freezegun",
    ],
)
