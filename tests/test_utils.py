"""Formatters — pure, deterministic, used all over the UI."""
import modules.utils as U


def test_format_size():
    assert U.format_size(0) == "0 B"
    assert U.format_size(1536) == "1.5 KB"
    assert U.format_size(5_368_709_120) == "5.0 GB"


def test_format_duration():
    assert U.format_duration(0) == "0:00"
    assert U.format_duration(59) == "0:59"
    assert U.format_duration(3661) == "1:01:01"


def test_format_time():
    assert U.format_time(45) == "45s"
    assert U.format_time(3661) == "1h 1m 1s"
