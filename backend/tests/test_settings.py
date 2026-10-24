import pytest
from pydantic import ValidationError

from app.settings import Settings


def test_consensus_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.devig_method == "multiplicative"
    assert settings.one_sided_overround == 0.10
    assert settings.consensus_half_life_minutes == 45
    assert settings.quote_keepalive_minutes == 45
    assert settings.stale_quote_minutes == 90


def test_a_live_quote_is_always_re_stored_well_before_it_goes_stale() -> None:
    """The keep-alive has to be shorter than the stale limit, or a quiet book would go stale."""
    settings = Settings(_env_file=None)
    assert settings.quote_keepalive_minutes * 2 <= settings.stale_quote_minutes


def test_settings_can_be_set_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVIG_METHOD", "power")
    monkeypatch.setenv("ONE_SIDED_OVERROUND", "0.15")
    monkeypatch.setenv("QUOTE_KEEPALIVE_MINUTES", "30")
    settings = Settings(_env_file=None)
    assert (settings.devig_method, settings.one_sided_overround) == ("power", 0.15)
    assert settings.quote_keepalive_minutes == 30


def test_the_word_none_turns_the_one_sided_margin_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONE_SIDED_OVERROUND", "none")
    assert Settings(_env_file=None).one_sided_overround is None


def test_an_unknown_de_vig_method_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVIG_METHOD", "logit")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
