import pytest
from app.services.runtime_schedule_service import _day_matches


def test_day_matches_daily():
    for wd in range(7):
        assert _day_matches("daily", wd) is True


def test_day_matches_weekdays():
    assert _day_matches("weekdays", 0) is True   # Montag
    assert _day_matches("weekdays", 4) is True   # Freitag
    assert _day_matches("weekdays", 5) is False  # Samstag
    assert _day_matches("weekdays", 6) is False  # Sonntag


def test_day_matches_weekends():
    assert _day_matches("weekends", 5) is True   # Samstag
    assert _day_matches("weekends", 6) is True   # Sonntag
    assert _day_matches("weekends", 0) is False  # Montag


def test_day_matches_unknown():
    assert _day_matches("unknown", 0) is False
