import importlib.util
import json

import pytest


def cli_module():
    assert importlib.util.find_spec("sidera_product.cli") is not None, (
        "N17 must supply the explicit native product CLI"
    )
    from sidera_product import cli

    return cli


def test_open_uses_selected_local_url_without_constructing_an_owner(
    settings_values, monkeypatch
):
    cli = cli_module()
    from sidera_product.config import ProductSettings

    settings = ProductSettings.model_validate(settings_values)
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(
        cli,
        "build_product_runtime",
        lambda *_: pytest.fail("open constructed an owner"),
    )
    assert cli.main(["open"], settings=settings) == 0
    assert opened == ["http://127.0.0.1:18080"]


def test_serve_has_one_inert_owner_and_no_reload_or_implicit_migration(
    settings_values, no_database_connections, monkeypatch
):
    cli = cli_module()
    from sidera_agents import bootstrap

    from sidera_product.config import ProductSettings

    calls = []

    def run(app, **kwargs):
        calls.append(kwargs)
        runtime = app.state.product_runtime
        assert runtime.status()["lifecycle"] == "constructed"
        assert runtime.close(grace_seconds=0) is True

    monkeypatch.setattr(cli.uvicorn, "run", run)
    monkeypatch.setattr(
        bootstrap.AgentService, "migrate", lambda *_: pytest.fail("serve migrated")
    )
    assert (
        cli.main(["serve"], settings=ProductSettings.model_validate(settings_values))
        == 0
    )
    assert calls == [
        {"host": "127.0.0.1", "port": 18080, "workers": 1, "reload": False}
    ]


def test_default_cli_setup_requires_real_local_session_before_any_write(
    settings_values, no_database_connections, capsys
):
    cli = cli_module()
    from sidera_product.config import ProductSettings

    assert (
        cli.main(
            [
                "workspace",
                "configure",
                "--workspace",
                str(settings_values["workspace_id"]),
                "--repository",
                "/untrusted/repo",
            ],
            settings=ProductSettings.model_validate(settings_values),
        )
        == 1
    )
    assert (
        json.loads(capsys.readouterr().err)["error"]["code"] == "local_session_required"
    )


def test_profile_ref_codec_requires_exact_existing_dto_and_workspace(settings_values):
    cli = cli_module()
    from sidera_agents.contracts import ProfileRef

    value = {
        "workspace_id": str(settings_values["workspace_id"]),
        "profile_id": "00000000-0000-0000-0000-000000000123",
        "revision": 7,
    }
    ref = cli.parse_planner_profile(json.dumps(value), settings_values["workspace_id"])
    assert isinstance(ref, ProfileRef)
    assert ref.revision == 7
    for wrong in (
        {**value, "revision": 0},
        {**value, "head": True},
        {**value, "workspace_id": "00000000-0000-0000-0000-000000000456"},
    ):
        with pytest.raises(cli.ProductCLIError, match="invalid_planner_profile"):
            cli.parse_planner_profile(
                json.dumps(wrong), settings_values["workspace_id"]
            )


def test_verification_codec_preserves_exact_checks_without_execution(monkeypatch):
    cli = cli_module()
    from sidera_control_plane.domain.result_integration import CheckSpecV1

    check = {
        "key": "owned-check",
        "version": 1,
        "argv": ["explicit-test-check"],
        "cwd": ".",
        "environment_ref": "controlled",
        "prepare_argvs": [],
        "timeout_seconds": 10,
        "success_exit_codes": [0],
        "report_paths": [],
        "required": True,
    }
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("checks executed")
    )
    assert cli.parse_verification_checks(json.dumps([check])) == (
        CheckSpecV1.from_mapping(check),
    )
    assert cli.parse_verification_checks(None) is None
    for wrong in (
        "[]",
        "{}",
        json.dumps([{**check, "argv": []}]),
        "INVALID_CHECK_SECRET_SENTINEL",
    ):
        with pytest.raises(cli.ProductCLIError) as error:
            cli.parse_verification_checks(wrong)
        assert "INVALID_CHECK_SECRET_SENTINEL" not in str(error.value)


def test_credential_migration_uses_installed_resources_filtered_env_and_timeout(
    settings_values, monkeypatch
):
    cli = cli_module()
    from sidera_product.config import ProductSettings

    settings = ProductSettings.model_validate(settings_values)
    monkeypatch.setenv(
        "SIDERA_CONFIGURATION_TEST_DATABASE_URL", "HOSTILE_OLD_TARGET_SENTINEL"
    )
    monkeypatch.setenv("PGHOST", "untrusted.example")
    captured = []

    def run(argv, **kwargs):
        captured.append((argv, kwargs))
        return cli.subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)
    cli.migrate_credentials(settings)
    argv, options = captured[0]
    assert argv[1:3] == ["-m", "alembic"]
    assert "site-packages" in argv[4]
    assert argv[-2:] == ["upgrade", "head"]
    assert (
        options["env"]["SIDERA_CONFIGURATION_DATABASE_URL"]
        == settings.credential_database_url.get_secret_value()
    )
    assert "SIDERA_CONFIGURATION_TEST_DATABASE_URL" not in options["env"]
    assert "PGHOST" not in options["env"]
    assert options["timeout"] == 60
    assert options["capture_output"] is True
    assert options["check"] is False
    assert (
        cli.os.environ["SIDERA_CONFIGURATION_TEST_DATABASE_URL"]
        == "HOSTILE_OLD_TARGET_SENTINEL"
    )


def test_migration_failure_never_relays_driver_or_dsn_secrets(
    settings_values, monkeypatch
):
    cli = cli_module()
    from sidera_product.config import ProductSettings

    def run(argv, **kwargs):
        return cli.subprocess.CompletedProcess(
            argv, 1, stdout="DSN_SECRET_SENTINEL", stderr="KEY_SECRET_SENTINEL"
        )

    monkeypatch.setattr(cli.subprocess, "run", run)
    with pytest.raises(
        cli.ProductCLIError, match="credential_migration_failed"
    ) as error:
        cli.migrate_credentials(ProductSettings.model_validate(settings_values))
    assert "SECRET_SENTINEL" not in str(error.value)


def test_invalid_cli_argument_does_not_echo_secret(capsys):
    cli = cli_module()
    with pytest.raises(SystemExit) as error:
        cli.main(["UNKNOWN_CLI_SECRET_SENTINEL"])
    assert error.value.code == 2
    assert "UNKNOWN_CLI_SECRET_SENTINEL" not in capsys.readouterr().err


@pytest.mark.parametrize("outcome", ["false", "raises", "complete"])
def test_actual_cli_result_requires_confirmed_owned_cleanup(
    settings_values, monkeypatch, capsys, outcome
):
    cli = cli_module()
    from sidera_product.config import ProductSettings

    class Owner:
        def __init__(self):
            self.closes = 0

        def inspect_readiness(self):
            return {
                "product": True,
                "agents": True,
                "control_plane": True,
                "credentials": True,
            }

        def status(self):
            return {"lifecycle": "constructed"}

        def close(self):
            self.closes += 1
            if outcome == "raises":
                raise RuntimeError("RAW_CLI_SECRET_SENTINEL")
            return outcome == "complete"

    owner = Owner()
    monkeypatch.setattr(cli, "build_product_runtime", lambda _: owner)
    result = cli.main(
        ["preflight", "--json"],
        settings=ProductSettings.model_validate(settings_values),
    )
    output = capsys.readouterr()
    assert owner.closes == 1
    assert "RAW_CLI_SECRET_SENTINEL" not in output.err
    assert "postgresql" not in output.out
    if outcome == "complete":
        assert result == 0 and output.err == ""
    else:
        assert result != 0
        assert json.loads(output.err)["error"]["code"] == "product_cleanup_pending"
