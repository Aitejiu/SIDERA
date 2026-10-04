import importlib.util
from uuid import UUID

import pytest
from pydantic import ValidationError


def product_settings():
    assert importlib.util.find_spec("sidera_product") is not None, (
        "N17 must supply the product settings/composition entrypoint"
    )
    from sidera_product.config import ProductSettings

    return ProductSettings


def test_product_maps_explicit_cp_settings_not_ambient_legacy_flags(
    settings_values, monkeypatch
):
    settings_class = product_settings()
    monkeypatch.setenv("SIDERA_DATABASE_URL", "HOSTILE_OLD_DATABASE_SENTINEL")
    monkeypatch.setenv("SIDERA_INTERVIEW_DEMO_MANIFEST", "/hostile/old-manifest")
    monkeypatch.setenv("SIDERA_RESULT_INPUTS_ENABLED", "false")
    monkeypatch.setenv("SIDERA_RESULT_INTEGRATION_ENABLED", "false")
    settings = settings_class.model_validate(settings_values)
    cp = settings.control_plane_settings()
    assert cp.database_url == settings_values["cp_database_url"]
    assert cp.local_actor_type == "user"
    assert cp.local_actor_id == "n17-test-user"
    assert cp.interview_demo_manifest is None
    assert cp.result_inputs_enabled is True
    assert cp.result_integration_enabled is True
    assert cp.result_repository_path is None
    assert cp.workspace_root == settings_values["workspace_root"]
    assert cp.artifact_root == settings_values["artifact_root"]
    assert settings.close_grace_seconds == 30.0


def test_product_explicit_disabled_result_options_reach_real_cp_settings(
    settings_values,
):
    settings = product_settings().model_validate(
        {
            **settings_values,
            "result_inputs_enabled": False,
            "result_integration_enabled": False,
        }
    )
    cp = settings.control_plane_settings()
    assert cp.result_inputs_enabled is False
    assert cp.result_integration_enabled is False
    assert cp.result_repository_path is None


@pytest.mark.parametrize(
    "extra",
    [
        {"bind_host": "0.0.0.0"},
        {"workers": 2},
        {"reload": True},
        {"close_grace_seconds": -1},
        {"close_grace_seconds": float("inf")},
        {"local_principal_id": ""},
        {"workspace_root": "relative/workspace"},
        {"web_origin": "https://untrusted.example"},
    ],
)
def test_product_rejects_unsafe_owner_or_location_configuration(settings_values, extra):
    with pytest.raises(ValidationError):
        product_settings().model_validate({**settings_values, **extra})


def test_cp_inherited_lease_validator_is_not_bypassed(settings_values):
    from sidera_control_plane.config import Settings as ControlPlaneSettings

    settings = product_settings().model_validate(settings_values)
    cp = settings.control_plane_settings()
    assert isinstance(cp, ControlPlaneSettings)
    with pytest.raises(ValidationError):
        type(cp)(
            database_url=cp.database_url,
            assignment_lease_seconds=10,
            assignment_lease_renew_interval_seconds=5,
        )


def test_product_required_inputs_cannot_be_inferred_from_old_env(monkeypatch):
    settings_class = product_settings()
    monkeypatch.setenv("SIDERA_DATABASE_URL", "OLD_TARGET_SECRET_SENTINEL")
    monkeypatch.setenv("SIDERA_AGENT_DATABASE_URL", "OTHER_OLD_TARGET")
    with pytest.raises(ValidationError) as error:
        settings_class()
    assert "OLD_TARGET_SECRET_SENTINEL" not in str(error.value)
    assert "OTHER_OLD_TARGET" not in str(error.value)


def test_product_validation_and_repr_do_not_echo_dsn_or_secret(settings_values):
    settings_class = product_settings()
    values = {
        **settings_values,
        "product_database_url": "DSN_SECRET_SENTINEL",
        "selected_secrets": {"SELECTED_KEY": "CREDENTIAL_SECRET_SENTINEL"},
    }
    with pytest.raises(ValidationError) as error:
        settings_class.model_validate(values)
    assert "DSN_SECRET_SENTINEL" not in str(error.value)
    assert "CREDENTIAL_SECRET_SENTINEL" not in str(error.value)
    good = settings_class.model_validate(
        {**settings_values, "selected_secrets": values["selected_secrets"]}
    )
    assert str(settings_values["product_database_url"]) not in repr(good)
    assert "CREDENTIAL_SECRET_SENTINEL" not in repr(good)
    assert good.deployment_namespace == UUID("898937f8-0a89-51ea-bfee-563b8524f4ad")


def test_real_product_environment_decodes_only_strict_scalars(
    settings_values, monkeypatch
):
    monkeypatch.setenv("SIDERA_PRODUCT_PORT", "18081")
    monkeypatch.setenv("SIDERA_PRODUCT_WORKERS", "1")
    monkeypatch.setenv("SIDERA_PRODUCT_RELOAD", "false")
    monkeypatch.setenv("SIDERA_PRODUCT_RESULT_INPUTS_ENABLED", "false")
    monkeypatch.setenv("SIDERA_PRODUCT_MANAGED_CREDENTIALS_ENABLED", "false")
    settings = product_settings()(**settings_values)
    assert settings.port == 18081
    assert settings.workers == 1
    assert settings.reload is False
    assert settings.managed_credentials_enabled is False
    assert settings.control_plane_settings().result_inputs_enabled is False


@pytest.mark.parametrize(
    "name,value",
    [
        ("workers", "2"),
        ("workers", "true"),
        ("workers", "1.0"),
        ("workers", "[]"),
        ("reload", "true"),
        ("reload", "1"),
        ("port", "INVALID_ENV_SECRET_SENTINEL"),
    ],
)
def test_invalid_environment_scalars_remain_strict_safe_errors(
    settings_values, monkeypatch, name, value
):
    monkeypatch.setenv(f"SIDERA_PRODUCT_{name.upper()}", value)
    with pytest.raises(ValidationError) as error:
        product_settings()(**settings_values)
    assert "INVALID_ENV_SECRET_SENTINEL" not in str(error.value)


@pytest.mark.parametrize("value", [True, 1.0])
def test_owner_count_does_not_coerce_non_string_inputs(settings_values, value):
    with pytest.raises(ValidationError):
        product_settings().model_validate({**settings_values, "workers": value})


@pytest.mark.parametrize("name", ["web_origin", "knowledge_base_url"])
@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1:notaport",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://[::1",
    ],
)
def test_loopback_http_locations_require_a_valid_port(settings_values, name, location):
    with pytest.raises(ValidationError):
        product_settings().model_validate({**settings_values, name: location})


@pytest.mark.parametrize("location", ["http://127.0.0.1", "http://[::1]:18081"])
def test_valid_loopback_http_locations_keep_explicit_ports(settings_values, location):
    settings = product_settings().model_validate(
        {**settings_values, "web_origin": location}
    )
    assert settings.web_origin == location
