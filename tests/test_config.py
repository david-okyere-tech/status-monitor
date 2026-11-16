import json
import textwrap

import pytest

from status_monitor.cli import parse_args, settings_from_namespace
from status_monitor.config import ConfigError, load_config


LEGACY = {
    "global": {
        "expected_statuses": [200],
        "timeout": 10,
        "retries": 2,
        "verify_ssl": True,
        "alert_threshold": 1,
    },
    "targets": [
        {"url": "https://example.com"},
        {"url": "https://internal.corp.local", "verify_ssl": False, "alert_threshold": 3},
    ],
}


def test_legacy_json_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(LEGACY))
    app = load_config(str(path))
    assert len(app.targets) == 2
    assert app.targets[0].url == "https://example.com"
    assert app.targets[0].expected_statuses == [200]
    assert app.targets[0].verify_ssl is True
    assert app.targets[1].verify_ssl is False
    assert app.targets[1].alert_threshold == 3
    assert app.targets[1].timeout == 10


def test_yaml_and_toml_round_out_the_same_shape(tmp_path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            """
            global:
              interval: 15
              timeout: 4
              ssl_warn_days: 14
            alerts:
              email: ops@example.com
              cooldown_checks: 2
              webhooks:
                - https://example.com/hook
                - url: https://hooks.slack.com/services/T/B/X
                  type: slack
            targets:
              - name: Example
                url: https://example.com
                contains: Example Domain
              - name: DNS
                type: tcp
                host: 1.1.1.1
                port: 443
              - name: Gateway
                type: ping
                host: 9.9.9.9
            """
        )
    )
    app = load_config(str(yaml_path))
    assert app.interval == 15
    assert app.email == "ops@example.com"
    assert app.cooldown_checks == 2
    assert [hook.kind for hook in app.webhooks] == ["generic", "slack"]
    assert app.targets[0].contains == ["Example Domain"]
    assert app.targets[0].ssl_warn_days == 14
    assert app.targets[1].url == "tcp://1.1.1.1:443"
    assert app.targets[2].check_type == "ping"

    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        textwrap.dedent(
            """
            [global]
            interval = 30
            timeout = 5

            [[targets]]
            name = "Example"
            url = "https://example.com"
            contains = "Example Domain"
            """
        )
    )
    toml_app = load_config(str(toml_path))
    assert toml_app.interval == 30
    assert toml_app.targets[0].name == "Example"
    assert toml_app.targets[0].timeout == 5


def test_rejects_unknown_keys_duplicates_and_bad_values(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"targets": [{"url": "https://example.com", "timeuot": 1}]}))
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(str(path))

    path.write_text(
        json.dumps(
            {
                "targets": [
                    {"url": "https://example.com"},
                    {"url": "https://example.com"},
                ]
            }
        )
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(str(path))

    path.write_text(json.dumps({"targets": [{"url": "https://example.com", "retries": -1}]}))
    with pytest.raises(ConfigError, match="retries"):
        load_config(str(path))

    path.write_text(json.dumps({"targets": [{"url": "https://example.com", "method": "HEAD", "contains": "x"}]}))
    with pytest.raises(ConfigError, match="GET"):
        load_config(str(path))

    path.write_text(json.dumps({"targets": [{"url": "https://example.com", "regex": "["}]}))
    with pytest.raises(ConfigError, match="regular expression"):
        load_config(str(path))

    path.write_text(json.dumps({"targets": [{"type": "tcp", "host": "-n", "port": 1}]}))
    with pytest.raises(ConfigError, match="invalid host"):
        load_config(str(path))


def test_cli_timeout_does_not_override_config_targets(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"global": {"interval": 15}, "targets": [{"url": "https://example.com", "timeout": 3}]}))
    _command, namespace = parse_args(
        ["--config", str(path), "--timeout", "9", "--interval", "4", "--url", "https://other.example"]
    )
    settings = settings_from_namespace(namespace)
    assert settings.interval == 4
    assert settings.targets[0].timeout == 3
    assert settings.targets[0].url == "https://example.com"
    assert settings.targets[1].url == "https://other.example"
    assert settings.targets[1].timeout == 9

    _command, inherited = parse_args(["--config", str(path)])
    assert settings_from_namespace(inherited).interval == 15


def test_shipped_examples_load():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("config.example.json", "config.example.yaml", "config.example.toml"):
        app = load_config(str(root / name))
        assert app.targets
