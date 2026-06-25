"""Tests for optional Wind adapter pure helpers."""

from worldquant_harness.wind_data import from_wind_code, is_wind_enabled, to_wind_code


class TestWindCodeConversion:
    def test_to_wind_from_baostock(self):
        assert to_wind_code("sh.600519") == "600519.SH"
        assert to_wind_code("sz.000001") == "000001.SZ"

    def test_to_wind_passthrough(self):
        assert to_wind_code("600519.SH") == "600519.SH"
        assert to_wind_code("000001.SZ") == "000001.SZ"

    def test_from_wind(self):
        assert from_wind_code("600519.SH") == "sh.600519"
        assert from_wind_code("000001.SZ") == "sz.000001"

    def test_wind_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("WORLDQUANT_HARNESS_DATA_SOURCE", raising=False)
        monkeypatch.delenv("WORLDQUANT_HARNESS_USE_WIND", raising=False)
        assert not is_wind_enabled()

    def test_wind_enabled_by_source(self, monkeypatch):
        monkeypatch.setenv("WORLDQUANT_HARNESS_DATA_SOURCE", "wind,baostock")
        monkeypatch.delenv("WORLDQUANT_HARNESS_USE_WIND", raising=False)
        assert is_wind_enabled()

