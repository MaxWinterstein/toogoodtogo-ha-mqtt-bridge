# Contributing

Thanks for taking the time to contribute! 🎉 Bug reports, feature ideas and pull
requests are all welcome.

## Reporting issues

- Search the [existing issues](https://github.com/MaxWinterstein/toogoodtogo-ha-mqtt-bridge/issues)
  first — your problem may already be tracked.
- For bugs, please include your `polling_schedule`, the relevant log output
  (with credentials redacted) and which install method you use (Home Assistant
  add-on, Docker, or native).
- Never paste your `settings.local.json`, `tokens.json` or any TooGoodToGo
  credentials into an issue.

## Development setup

This project uses [`uv`](https://docs.astral.sh/uv/) for dependency management
and [`Taskfile`](https://taskfile.dev/) as a task runner. The optional combo of
[`pkgx`](https://pkgx.dev/) + [`direnv`](https://direnv.net/) gives you the
fastest start (see the README), but plain `uv` works everywhere.

```bash
# install all dependencies (incl. dev tools) into a local .venv
uv sync --all-extras --dev

# install the git hooks
uv run pre-commit install

# run the bridge natively
task run        # or: uv run python toogoodtogo_ha_mqtt_bridge/main.py
```

Copy `toogoodtogo_ha_mqtt_bridge/settings.example.json` to
`settings.local.json` and fill in your MQTT broker and TooGoodToGo e-mail before
running.

## Before opening a pull request

Please run the full check suite and make sure it passes:

```bash
task check      # runs lock check, pre-commit (ruff), mypy and deptry
uv run pytest   # run the test suite
```

`pre-commit.ci` and the GitHub Actions workflows run the same checks on your PR,
so running them locally first saves a round-trip.

- Keep changes focused — one logical change per pull request.
- Add or update tests when you change behavior.
- New and changed code should be type-hinted (`mypy` runs in CI).

## License

By contributing, you agree that your contributions will be licensed under the
[GNU General Public License v3.0](LICENSE).
