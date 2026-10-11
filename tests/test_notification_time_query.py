from datetime import datetime, timezone

import pytest

from toposync.runtime.notifications.time_query import resolve_history_time


def test_relative_afternoon_uses_installation_date_not_utc_date():
    query, result = resolve_history_time("gato no bebedouro ontem à tarde", "America/Sao_Paulo",
                                        now=datetime(2026, 9, 27, 1, tzinfo=timezone.utc))
    assert query == "gato bebedouro"
    window = result["time_range"]
    assert window["created_after"] == datetime(2026, 9, 25, 15, tzinfo=timezone.utc).timestamp()
    assert window["created_before"] == datetime(2026, 9, 25, 21, tzinfo=timezone.utc).timestamp()
    assert window["basis"] == "notification_created_at"


@pytest.mark.parametrize("day,hours", [(datetime(2026, 3, 8, 18, tzinfo=timezone.utc), 23),
                                       (datetime(2026, 11, 1, 18, tzinfo=timezone.utc), 25)])
def test_relative_day_preserves_daylight_saving_length(day, hours):
    query, result = resolve_history_time("today", "America/New_York", now=day)
    assert query == ""
    window = result["time_range"]
    assert window["created_before"] - window["created_after"] == hours * 3600


@pytest.mark.parametrize("phrase,start,end", [("hoje de madrugada", 0, 6), ("hoje de manhã", 6, 12),
                                             ("today afternoon", 12, 18), ("hoje à noite", 18, 24)])
def test_documented_periods(phrase, start, end):
    _, result = resolve_history_time(phrase, "UTC", now=datetime(2026, 9, 27, 10, tzinfo=timezone.utc))
    midnight = datetime(2026, 9, 27, tzinfo=timezone.utc).timestamp()
    assert result["time_range"]["created_after"] == midnight + start * 3600
    assert result["time_range"]["created_before"] == midnight + end * 3600


def test_missing_zone_never_assumes_host_zone_and_unsupported_dates_remain_literal():
    assert resolve_history_time("gato ontem", None)[1] == {"reason": "installation_timezone_required"}
    assert resolve_history_time("gato ontem", "Unknown/Zone")[1] == {"reason": "installation_timezone_required"}
    assert resolve_history_time("gato semana passada", "UTC") == ("gato semana passada", None)
    assert resolve_history_time("pessoa com pacote", None) == ("pessoa com pacote", None)


@pytest.mark.parametrize("phrase", ["ontem à tarde gato no bebedouro", "  ontem à tarde gato no bebedouro  ", "gato no bebedouro ontem à tarde"])
def test_relative_period_at_either_end_has_the_same_meaning(phrase):
    now = datetime(2026, 9, 27, 1, tzinfo=timezone.utc)
    query, result = resolve_history_time(phrase, "America/Sao_Paulo", now=now)
    assert query == "gato bebedouro"
    assert result["time_range"]["created_after"] == datetime(2026, 9, 25, 15, tzinfo=timezone.utc).timestamp()
    assert result["time_range"]["created_before"] == datetime(2026, 9, 25, 21, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("phrase", ["ontem gato hoje", "gato ontem cachorro", "hojemente gato"])
def test_ambiguous_or_embedded_relative_period_remains_literal(phrase):
    assert resolve_history_time(phrase, "UTC") == (phrase, None)
