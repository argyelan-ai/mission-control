from datetime import datetime, timedelta, tzinfo, timezone

from app.utils import create_tracked_task, ensure_aware


class _NaiveTzinfo(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "naive-like"


def test_ensure_aware_treats_tzinfo_without_offset_as_naive_utc():
    value = datetime(2026, 3, 15, 12, 0, tzinfo=_NaiveTzinfo())

    result = ensure_aware(value)

    assert result.tzinfo == timezone.utc
    assert result.utcoffset() == timedelta(0)
    assert result.replace(tzinfo=None) == value.replace(tzinfo=None)


def test_ensure_aware_docstring_documents_naive_utc_interpretation():
    docstring = ensure_aware.__doc__

    assert docstring is not None
    assert "Args:" in docstring
    assert "dt:" in docstring
    assert "Returns:" in docstring
    assert "UTC" in docstring
    assert "aware" in docstring


def test_create_tracked_task_docstring_documents_args_and_return_value():
    docstring = create_tracked_task.__doc__

    assert docstring is not None
    assert "Args:" in docstring
    assert "coro:" in docstring
    assert "name:" in docstring
    assert "Returns:" in docstring
    assert "asyncio.Task" in docstring
