"""Optional Wind Oracle data adapter for local A-share research.

This module is deliberately disabled by default. Enable it with either:

    WORLDQUANT_HARNESS_DATA_SOURCE=wind
    WORLDQUANT_HARNESS_USE_WIND=1

Connection parameters are read from WORLDQUANT_HARNESS_WIND_DB_* environment variables.
If they are absent, the adapter can read a local MCP server config only when
WORLDQUANT_HARNESS_WIND_CONFIG_PATH is set.
"""

from __future__ import annotations

import ast
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

TRUTHY = {"1", "true", "yes", "y", "on"}
INDEX_CODES = {
    "hs300": "000300.SH",
    "zz500": "000905.SH",
    "csi500": "000905.SH",
    "csi1000": "000852.SH",
    "sz50": "000016.SH",
}


@dataclass(frozen=True)
class WindConfig:
    user: str
    password: str
    dsn: str
    encoding: str = "UTF-16"
    oracle_client_dir: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.user and self.password and self.dsn)


def is_wind_enabled() -> bool:
    source = os.environ.get("WORLDQUANT_HARNESS_DATA_SOURCE", "").lower()
    enabled = os.environ.get("WORLDQUANT_HARNESS_USE_WIND", "").lower() in TRUTHY
    return enabled or any(part.strip() == "wind" for part in source.split(","))


def _parse_local_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}

    tree = ast.parse(path.read_text(encoding="utf-8"))
    parsed: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        for name in names:
            if name in {"CONN_PARAMS", "_ORA_CLIENT_DIR"}:
                try:
                    parsed[name] = ast.literal_eval(node.value)
                except Exception as exc:
                    logger.debug("Unable to parse %s from %s: %s", name, path, exc)
    return parsed


def load_wind_config() -> WindConfig:
    config_path_raw = os.environ.get("WORLDQUANT_HARNESS_WIND_CONFIG_PATH", "").strip()
    local = _parse_local_config(Path(config_path_raw)) if config_path_raw else {}
    conn_params = local.get("CONN_PARAMS", {}) if isinstance(local.get("CONN_PARAMS"), dict) else {}

    return WindConfig(
        user=os.environ.get("WORLDQUANT_HARNESS_WIND_DB_USER", os.environ.get("WIND_DB_USER", conn_params.get("user", ""))),
        password=os.environ.get(
            "WORLDQUANT_HARNESS_WIND_DB_PASSWORD",
            os.environ.get("WIND_DB_PASSWORD", conn_params.get("password", "")),
        ),
        dsn=os.environ.get("WORLDQUANT_HARNESS_WIND_DB_DSN", os.environ.get("WIND_DB_DSN", conn_params.get("dsn", ""))),
        encoding=os.environ.get(
            "WORLDQUANT_HARNESS_WIND_DB_ENCODING",
            os.environ.get("WIND_DB_ENCODING", conn_params.get("encoding", "UTF-16")),
        ),
        oracle_client_dir=os.environ.get(
            "WORLDQUANT_HARNESS_ORACLE_CLIENT_DIR",
            os.environ.get("ORACLE_CLIENT_DIR", local.get("_ORA_CLIENT_DIR", "")),
        ),
    )


def _import_oracle_driver():
    try:
        import cx_Oracle  # type: ignore

        return cx_Oracle
    except ImportError:
        try:
            import oracledb  # type: ignore

            return oracledb
        except ImportError as exc:
            raise RuntimeError("Install cx_Oracle or oracledb to use Wind data") from exc


def _date_to_wind(date: str) -> str:
    return str(date)[:10].replace("-", "")


def to_wind_code(stock_code: str) -> str:
    code = str(stock_code).strip()
    if not code:
        return code
    if "." in code:
        left, right = code.split(".", 1)
        if left.lower() in {"sh", "sz"}:
            return f"{right}.{left.upper()}"
        if right.upper() in {"SH", "SZ"}:
            return f"{left}.{right.upper()}"
    if code.startswith(("sh", "sz")) and len(code) == 8:
        return f"{code[2:]}.{code[:2].upper()}"
    if code[0] in {"5", "6", "9"}:
        return f"{code}.SH"
    return f"{code}.SZ"


def from_wind_code(wind_code: str) -> str:
    code = str(wind_code).strip()
    if "." not in code:
        return to_wind_code(code).replace(".SH", ".sh")
    num, suffix = code.split(".", 1)
    prefix = "sh" if suffix.upper() == "SH" else "sz"
    return f"{prefix}.{num}"


def _chunked(values: list[str], size: int = 900):
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _in_clause(values: list[str], prefix: str) -> tuple[str, dict[str, str]]:
    binds = {f"{prefix}{i}": value for i, value in enumerate(values)}
    clause = ", ".join(f":{key}" for key in binds)
    return clause, binds


class WindDataFetcher:
    """Read A-share OHLCV, valuation, index members and benchmark returns from Wind."""

    def __init__(self, config: WindConfig | None = None):
        self.config = config or load_wind_config()
        self._driver = None
        self._conn = None

    def available(self) -> bool:
        return self.config.complete

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _get_conn(self):
        if not self.config.complete:
            raise RuntimeError("Wind DB config is incomplete")

        if self._conn is not None:
            try:
                self._conn.ping()
                return self._conn
            except Exception:
                self._conn = None

        driver = self._driver or _import_oracle_driver()
        self._driver = driver

        client_dir = self.config.oracle_client_dir
        if client_dir and os.name == "nt" and os.path.isdir(client_dir):
            try:
                os.add_dll_directory(client_dir)
            except Exception:
                pass
            try:
                driver.init_oracle_client(lib_dir=client_dir)
            except Exception:
                pass

        kwargs = {
            "user": self.config.user,
            "password": self.config.password,
            "dsn": self.config.dsn,
            "encoding": self.config.encoding,
        }
        try:
            self._conn = driver.connect(**kwargs)
        except TypeError:
            kwargs.pop("encoding", None)
            self._conn = driver.connect(**kwargs)
        return self._conn

    def _query(self, sql: str, params: dict[str, Any]) -> pd.DataFrame:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.arraysize = 10000
        try:
            cursor.execute(sql, params)
            columns = [desc[0].lower() for desc in cursor.description]
            parts = []
            while True:
                rows = cursor.fetchmany(10000)
                if not rows:
                    break
                parts.append(pd.DataFrame(rows, columns=columns))
        finally:
            cursor.close()
        if not parts:
            return pd.DataFrame()
        return pd.concat(parts, ignore_index=True)

    def fetch_index_members(self, universe: str, date: str | None = None) -> list[str]:
        index_code = INDEX_CODES.get(universe)
        if not index_code:
            return []
        as_of = _date_to_wind(date or pd.Timestamp.today().strftime("%Y-%m-%d"))
        sql = """
            SELECT S_CON_WINDCODE
            FROM windnew.AINDEXMEMBERS
            WHERE S_INFO_WINDCODE = :index_code
              AND S_CON_INDATE <= :as_of
              AND (S_CON_OUTDATE IS NULL OR S_CON_OUTDATE = '0' OR S_CON_OUTDATE > :as_of)
            ORDER BY S_CON_WINDCODE
        """
        df = self._query(sql, {"index_code": index_code, "as_of": as_of})
        if df.empty:
            return []
        return [from_wind_code(code) for code in df["s_con_windcode"].dropna().astype(str)]

    def fetch_stocks(self, stock_codes: list[str], start_date: str, end_date: str) -> pd.DataFrame | None:
        wind_codes = sorted({to_wind_code(code) for code in stock_codes if code})
        if not wind_codes:
            return None

        start_dt = _date_to_wind(start_date)
        end_dt = _date_to_wind(end_date)
        parts = []

        sql_template = """
            SELECT
                p.S_INFO_WINDCODE AS stock_code,
                p.TRADE_DT AS trade_date,
                NVL(p.S_DQ_ADJOPEN, p.S_DQ_OPEN) AS q_open,
                NVL(p.S_DQ_ADJHIGH, p.S_DQ_HIGH) AS high,
                NVL(p.S_DQ_ADJLOW, p.S_DQ_LOW) AS low,
                NVL(p.S_DQ_ADJCLOSE, p.S_DQ_CLOSE) AS close,
                p.S_DQ_VOLUME * 100 AS volume,
                p.S_DQ_AMOUNT * 1000 AS amount,
                p.S_DQ_PCTCHANGE AS pct_change,
                NVL(
                    p.S_DQ_AVGPRICE * NVL(p.S_DQ_ADJFACTOR, 1),
                    CASE WHEN p.S_DQ_VOLUME IS NOT NULL AND p.S_DQ_VOLUME <> 0
                         THEN p.S_DQ_AMOUNT * 1000 / (p.S_DQ_VOLUME * 100) * NVL(p.S_DQ_ADJFACTOR, 1)
                    END
                ) AS vwap,
                d.S_VAL_MV AS market_cap,
                d.S_DQ_MV AS float_market_cap,
                d.S_VAL_PE_TTM AS pe,
                d.S_VAL_PB_NEW AS pb,
                d.S_VAL_PS_TTM AS ps,
                d.S_DQ_TURN AS turnover_rate,
                d.TOT_SHR_TODAY AS shares,
                d.NET_PROFIT_PARENT_COMP_TTM AS net_income,
                d.NET_CASH_FLOWS_OPER_ACT_TTM AS cash_flow,
                d.OPER_REV_TTM AS revenue
            FROM windnew.AShareEODPrices p
            LEFT JOIN windnew.AShareEODDerivativeIndicator d
              ON p.S_INFO_WINDCODE = d.S_INFO_WINDCODE
             AND p.TRADE_DT = d.TRADE_DT
            WHERE p.TRADE_DT >= :start_dt
              AND p.TRADE_DT <= :end_dt
              AND p.S_INFO_WINDCODE IN ({codes})
            ORDER BY p.S_INFO_WINDCODE, p.TRADE_DT
        """

        for chunk in _chunked(wind_codes):
            clause, binds = _in_clause(chunk, "code")
            params = {"start_dt": start_dt, "end_dt": end_dt, **binds}
            parts.append(self._query(sql_template.format(codes=clause), params))

        data = pd.concat([part for part in parts if not part.empty], ignore_index=True) if parts else pd.DataFrame()
        if data.empty:
            return None

        data = data.rename(columns={"q_open": "open"})
        data["stock_code"] = data["stock_code"].map(from_wind_code)
        data["trade_date"] = pd.to_datetime(data["trade_date"], format="%Y%m%d", errors="coerce")
        numeric_cols = [col for col in data.columns if col not in {"stock_code", "trade_date"}]
        for col in numeric_cols:
            data[col] = pd.to_numeric(data[col], errors="coerce")
        if "pct_change" not in data or data["pct_change"].isna().all():
            data["pct_change"] = data.groupby("stock_code")["close"].pct_change() * 100
        data["cap"] = data["market_cap"]
        data = data.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)
        return data

    def fetch_benchmark_returns(self, benchmark: str, start_date: str, end_date: str) -> pd.Series | None:
        index_code = INDEX_CODES.get(benchmark, INDEX_CODES["hs300"])
        sql = """
            SELECT TRADE_DT AS trade_date, S_DQ_CLOSE AS close, S_DQ_PCTCHANGE AS pct_change
            FROM windnew.AINDEXEODPRICES
            WHERE S_INFO_WINDCODE = :index_code
              AND TRADE_DT >= :start_dt
              AND TRADE_DT <= :end_dt
            ORDER BY TRADE_DT
        """
        df = self._query(
            sql,
            {"index_code": index_code, "start_dt": _date_to_wind(start_date), "end_dt": _date_to_wind(end_date)},
        )
        if df.empty:
            return None
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["pct_change"] = pd.to_numeric(df["pct_change"], errors="coerce")
        daily_return = df["pct_change"] / 100.0
        if daily_return.isna().all():
            daily_return = df["close"].pct_change()
        series = pd.Series(daily_return.to_numpy(), index=df["trade_date"], name=benchmark).dropna()
        return series if len(series) > 1 else None


def get_wind_universe(universe: str, date: str | None = None) -> list[str]:
    if not is_wind_enabled():
        return []
    fetcher = WindDataFetcher()
    try:
        return fetcher.fetch_index_members(universe, date)
    except Exception as exc:
        logger.warning("Wind universe fetch failed for %s: %s", universe, exc)
        return []
    finally:
        fetcher.close()


def fetch_wind_benchmark_returns(benchmark: str, start_date: str, end_date: str) -> pd.Series | None:
    if not is_wind_enabled():
        return None
    fetcher = WindDataFetcher()
    try:
        return fetcher.fetch_benchmark_returns(benchmark, start_date, end_date)
    except Exception as exc:
        logger.warning("Wind benchmark fetch failed for %s: %s", benchmark, exc)
        return None
    finally:
        fetcher.close()
