"""Configuración: proxies de confianza y validaciones de arranque (Fase 3.1).

Estas pruebas cubren el contrato de ``TRUST_PROXY``/``TRUSTED_PROXIES``: sin
lista de proxies la cabecera ``X-Forwarded-For`` no se honra (S-04), y una
combinación incoherente falla al arrancar en lugar de quedarse sin efecto
(la lección de D-03).
"""
from __future__ import annotations

import pytest

import config


def _settings(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config.reset_settings()
    return config.get_settings()


class TestTrustProxy:
    def test_off_by_default(self):
        settings = config.get_settings()
        assert settings.trust_proxy is False
        assert settings.trusted_proxies == ()

    def test_parses_a_cidr_list(self, monkeypatch):
        settings = _settings(
            monkeypatch,
            TRUST_PROXY="true",
            TRUSTED_PROXIES="127.0.0.1/32, 10.0.0.0/8; 192.168.1.0/24",
        )
        assert settings.trust_proxy is True
        assert settings.trusted_proxies == (
            "127.0.0.1/32",
            "10.0.0.0/8",
            "192.168.1.0/24",
        )

    def test_normalises_a_bare_ip_to_a_host_network(self, monkeypatch):
        settings = _settings(monkeypatch, TRUST_PROXY="1", TRUSTED_PROXIES="172.16.5.4")
        assert settings.trusted_proxies == ("172.16.5.4/32",)

    def test_rejects_a_malformed_cidr(self, monkeypatch):
        with pytest.raises(config.ConfigError):
            _settings(monkeypatch, TRUST_PROXY="true", TRUSTED_PROXIES="no-es-una-red")

    def test_trust_proxy_without_a_list_is_a_config_error(self, monkeypatch):
        settings = _settings(monkeypatch, TRUST_PROXY="true")
        with pytest.raises(config.ConfigError):
            settings.validate()

    def test_proxy_list_without_trust_proxy_is_a_config_error(self, monkeypatch):
        settings = _settings(monkeypatch, TRUSTED_PROXIES="10.0.0.0/8")
        with pytest.raises(config.ConfigError):
            settings.validate()

    def test_a_coherent_pair_validates(self, monkeypatch):
        settings = _settings(
            monkeypatch, TRUST_PROXY="true", TRUSTED_PROXIES="127.0.0.1/32"
        )
        settings.validate()  # no lanza


class TestMoneySettings:
    """Fase 4.3 (H-*): gas, tope de apuesta y ventanas son configuración."""

    def test_defaults_match_the_documented_values(self):
        settings = config.get_settings()
        assert settings.max_stake_nano == 5_000_000_000_000
        assert settings.bet_gas_nano == 30_000_000
        assert settings.create_gas_nano == 50_000_000
        assert settings.admin_gas_nano == 50_000_000
        assert settings.claim_gas_nano == 20_000_000
        assert settings.min_market_duration_min == 15
        assert settings.max_market_duration_days == 365
        assert settings.tx_ttl_seconds == 300

    def test_env_overrides_them(self, monkeypatch):
        settings = _settings(
            monkeypatch,
            MAX_STAKE_NANO="1000000000",
            BET_GAS_NANO="40000000",
            MIN_MARKET_DURATION_MIN="5",
            TX_TTL_SECONDS="600",
        )
        assert settings.max_stake_nano == 1_000_000_000
        assert settings.bet_gas_nano == 40_000_000
        assert settings.min_market_duration_min == 5
        assert settings.tx_ttl_seconds == 600

    def test_max_stake_cannot_be_below_the_minimum(self, monkeypatch):
        settings = _settings(monkeypatch, MAX_STAKE_NANO="1")
        with pytest.raises(config.ConfigError):
            settings.validate()

    def test_durations_must_be_coherent(self, monkeypatch):
        settings = _settings(
            monkeypatch, MIN_MARKET_DURATION_MIN="1441", MAX_MARKET_DURATION_DAYS="1"
        )
        with pytest.raises(config.ConfigError):
            settings.validate()


class TestValidation:
    """Los errores de configuración fallan al arrancar, no en producción."""

    def test_unknown_mode(self, monkeypatch):
        with pytest.raises(config.ConfigError):
            _settings(monkeypatch, BET_MODE="casino").validate()

    def test_escrow_mode_requires_an_address(self, monkeypatch):
        monkeypatch.delenv("ESCROW_ADDRESS", raising=False)
        with pytest.raises(config.ConfigError):
            _settings(monkeypatch, BET_MODE="escrow").validate()

    def test_escrow_mode_requires_https(self, monkeypatch):
        settings = _settings(monkeypatch, PUBLIC_BASE_URL="http://betpoly.test")
        with pytest.raises(config.ConfigError):
            settings.validate()

    def test_dev_sessions_are_forbidden_in_production(self, monkeypatch):
        settings = _settings(
            monkeypatch, ENVIRONMENT="production", ALLOW_DEV_SESSION="1", WEB_ENABLED="1"
        )
        with pytest.raises(config.ConfigError):
            settings.validate()

    def test_production_requires_a_real_secret(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        settings = _settings(monkeypatch, ENVIRONMENT="production")
        with pytest.raises(config.ConfigError):
            settings.validate()

    def test_unknown_timezone(self, monkeypatch):
        with pytest.raises(config.ConfigError):
            _settings(monkeypatch, TIMEZONE="Marte/Olympus").validate()

    def test_non_numeric_points_are_rejected(self, monkeypatch):
        with pytest.raises(config.ConfigError):
            _settings(monkeypatch, STARTING_POINTS="mil")

    def test_out_of_range_points_are_rejected(self, monkeypatch):
        with pytest.raises(config.ConfigError):
            _settings(monkeypatch, MIN_BET_POINTS="0")

    def test_admin_ids_must_be_numeric(self, monkeypatch):
        with pytest.raises(config.ConfigError):
            _settings(monkeypatch, ADMIN_IDS="1001,root")
