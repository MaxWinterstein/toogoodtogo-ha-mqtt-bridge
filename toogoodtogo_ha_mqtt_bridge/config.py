from dynaconf import Dynaconf, Validator

msg = "Settings object '{name}' not found. Did you create a settings.local.json?"

settings = Dynaconf(
    envvar_prefix="DYNACONF",
    settings_files=["settings.json", "settings.local.json", ".secrets.toml"],
)

settings.validators.register(
    Validator("tgtg", must_exist=True, messages={"must_exist_true": msg}),
)
