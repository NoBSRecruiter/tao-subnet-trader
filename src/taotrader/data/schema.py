"""taotrader/data/schema.py - storage schema (DESIGN.md section 7): DDL, Parquet column specs, snapshot <-> rows.

Single source for every storage layout WP3 owns:

- LAKE_DDL, JOURNAL_DDL and STATE_DDL are the section 7.1 / 7.2 / 7.3 SQL blocks, verbatim (tests/data/test_schema.py
  compares them with DESIGN.md). LAKE_TABLES is parsed from LAKE_DDL, so the Parquet column specs cannot drift.
- One documented extension (ADR request in the WP3 report): each snapshot table (snap_global, snap_subnet, snap_hotkey)
  carries one extra nullable column `exact_json`. It is NULL in the normal case. When a value cannot be held exactly
  by its section 7.1 column (a Decimal that is not raw/2^k of its fixed-point format, a TaoWeight that is not
  raw/u64::MAX under the EXACT context, a SafeFloat/V1 share count whose mantissa exceeds 38 digits, an integer
  outside the column range) the column holds the nearest representable value (or NULL) for analytics, and
  `exact_json` maps the column name to the exact canonical text. A snapshot therefore always round-trips
  digest-identically (section 11 WP3 acceptance), whatever decoder produced it.
- Parquet has no 128-bit integer: a HUGEINT column is stored as DECIMAL(38,0) and the DuckDB views cast it back to
  HUGEINT. |value| must stay below 10^38 (else the exact_json fallback applies; generic tables raise).

Snapshot digest convention (section 6.8 step 9): `digest = blake2b-128(core.codec.canonical_bytes(snapshot with
digest=""))`, i.e. `codec.digest(dataclasses.replace(snap, digest=""))`, 32 hex characters. WP1 must build the
digest the same way; `with_digest` fills an empty digest and rejects a mismatching one (fail closed).

Fixed-point conventions used for the *_raw columns (brief section 9 storage table; core.fixed.EXACT decoding):
I96F32 / U96F32 = raw / 2^32 (moving_alpha, moving_price, root_prop, miner_burned); U64F64 = raw / 2^64 (gate_bar,
fast_moving_price); TaoWeight = EXACT.divide(raw, u64::MAX); shares = mantissa * 10^exp with the mantissa stripped of
trailing zeros (V2 SafeFloat directly, V1 U64F64 converted exactly when it fits).
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from operator import itemgetter
from typing import Any, Final

from ..core import codec
from ..core.errors import DecodeError
from ..core.fixed import EXACT, floor_int
from ..core.state import (
    ChainGlobals,
    ChainSnapshot,
    HotkeyIdx,
    MetagraphLite,
    PoolKind,
    PoolState,
    Quality,
    ReadPlan,
    SubnetState,
)
from ..core.units import U64_MAX, AlphaRao, Block, BlockHash, Coldkey, Hotkey, NetUid, Rao, SubnetKey

SCHEMA_VERSION: Final[int] = 1          # manifest.schema_version of every chunk written by this code
DEFAULT_DECODER_VERSION: Final[int] = 1  # manifest/snapshot decoder_version when the caller does not pass WP1's value

# ------------------------------------------------------------------------------------------------ section 7.1
LAKE_DDL: Final[str] = """\
-- global state, one row per snapshot block
CREATE TABLE snap_global (
  block UBIGINT PRIMARY KEY, block_hash VARCHAR, ts_ms UBIGINT, plan UTINYINT,           -- 1 HEAD, 2 FULL
  spec_version USMALLINT, tx_version USMALLINT, total_issuance UBIGINT, block_emission UBIGINT,
  moving_alpha_raw HUGEINT, gate_bar_raw HUGEINT, gate_rank USMALLINT, gate_exponent UTINYINT,
  tao_weight_raw UBIGINT, root_tao UBIGINT, owner_cut_u16 USMALLINT, subnet_limit USMALLINT,
  immunity_period UBIGINT, network_rate_limit UBIGINT, last_reg_block UBIGINT, last_lock_cost UBIGINT,
  min_lock_cost UBIGINT, lock_reduction_interval UBIGINT, tao_in_refund_block UBIGINT, nominator_min_stake UBIGINT,
  cleanup_queue_len USMALLINT, n_nonroot_networks USMALLINT, safe_mode_until UBIGINT, shorts_enabled BOOLEAN,
  runtime_prune_target USMALLINT, digest VARCHAR, quality_or UINTEGER, decoder_version USMALLINT);

-- per subnet generation per snapshot. PK (block, netuid). Generation = (netuid, reg_at).
CREATE TABLE snap_subnet (
  block UBIGINT, netuid USMALLINT, reg_at UBIGINT,
  pool_kind UTINYINT, tao UBIGINT, alpha_in UBIGINT, px_tao HUGEINT, px_alpha HUGEINT,   -- era-correct effective pool
  w_quote_e18 UBIGINT, fee_rate USMALLINT, reservoir_tao UBIGINT, reservoir_alpha UBIGINT,
  alpha_out UBIGINT, protocol_alpha UBIGINT,
  moving_price_raw HUGEINT, fast_moving_raw HUGEINT, root_prop_raw HUGEINT, miner_burned_raw HUGEINT,
  emission_enabled BOOLEAN, subtoken_enabled BOOLEAN, reg_allowed BOOLEAN, first_emission_block UBIGINT,
  tempo USMALLINT, last_epoch_block UBIGINT, ema_halving_blocks UINTEGER,
  tao_in_emission UBIGINT, excess_tao UBIGINT, alpha_out_emission UBIGINT, alpha_in_emission UBIGINT,
  tao_flow_cum BIGINT, volume_cum HUGEINT, owner_coldkey VARCHAR, owner_hotkey VARCHAR,
  owner_cut_enabled BOOLEAN, owner_cut_autolock BOOLEAN, total_alpha_staked UBIGINT, max_allowed_validators USMALLINT,
  consensus_mode UTINYINT,                                                                  -- NULL before spec 475
  mg_n_miners USMALLINT, mg_n_miner_coldkeys USMALLINT, mg_top1_coldkey_ppm UINTEGER, mg_n_permit_coldkeys USMALLINT,  -- LCW only
  escrow_alpha UBIGINT, escrow_block UBIGINT,                                               -- forward-fill source block
  owner_alpha UBIGINT, quality UINTEGER, decoder_version USMALLINT);

-- tracked hotkey share pools. PK (block, netuid, hotkey)
CREATE TABLE snap_hotkey (
  block UBIGINT, netuid USMALLINT, reg_at UBIGINT, hotkey VARCHAR,                          -- 0x + 64 hex
  total_alpha UBIGINT, shares_src UTINYINT,                                                 -- 1 = V1 U64F64, 2 = V2 SafeFloat
  shares_mantissa HUGEINT, shares_exp SMALLINT,                                             -- exact: shares = m * 10^e (V1 converted exactly)
  take_u16 USMALLINT, childkey_take_u16 USMALLINT, earns BOOLEAN, last_dividend UBIGINT);

-- membership listing (daily state_getKeysPaged) PK (block, netuid, hotkey)
CREATE TABLE dividend_keys (block UBIGINT, netuid USMALLINT, reg_at UBIGINT, hotkey VARCHAR);

-- generation lifecycle (built by data.collector + data.refine)
CREATE TABLE generation (
  netuid USMALLINT, reg_at UBIGINT,                                                         -- PK
  queued_block UBIGINT, added_block UBIGINT, start_call_block UBIGINT, first_seen UBIGINT, last_seen UBIGINT,
  end_block UBIGINT, end_kind VARCHAR,                                                      -- 'pruned' | 'dissolved' | 'open'
  end_refined BOOLEAN, lock_amount UBIGINT, seed_price_rao UBIGINT, seed_anomaly BOOLEAN,
  pre_end_tao UBIGINT, pre_end_alpha_in UBIGINT, pre_end_alpha_out UBIGINT, pre_end_protocol UBIGINT,
  pre_end_escrow UBIGINT, pre_end_total_staked UBIGINT, observed_payout_ratio DOUBLE);       -- FT10 calibration

-- derived chain events (lake copy of protocol.derive output at collection cadence)
CREATE TABLE chain_event (block UBIGINT, kind VARCHAR, netuid USMALLINT, reg_at UBIGINT, hotkey VARCHAR,
  flag BOOLEAN, amount HUGEINT, frac_ppm INTEGER, name VARCHAR, old VARCHAR, new VARCHAR);

CREATE TABLE registration (queued_block UBIGINT PRIMARY KEY, victim_netuid USMALLINT, victim_reg_at UBIGINT,
  new_reg_at UBIGINT, cost_ratio DOUBLE, lock_amount UBIGINT, blocks_since_prev UBIGINT, shielded BOOLEAN);
CREATE TABLE spec_boundary (spec_version USMALLINT PRIMARY KEY, setcode_block UBIGINT, first_logic_block UBIGINT);
CREATE TABLE calib (block UBIGINT, probe VARCHAR, netuid USMALLINT, model DOUBLE, chain DOUBLE, rel_err DOUBLE);
CREATE TABLE raw_rpc (block UBIGINT, call VARCHAR, request_sha VARCHAR, response_zstd BLOB);  -- optional layer 0 for re-decoding
-- optional enrichment (Taostats), joined by BLOCK to a generation, never by netuid alone
CREATE TABLE ext_trades (block UBIGINT, netuid USMALLINT, reg_at UBIGINT, side VARCHAR, coldkey VARCHAR,
  tao_rao UBIGINT, alpha_rao UBIGINT, extrinsic_id VARCHAR, seq USMALLINT);
CREATE TABLE ext_crosscheck (day DATE, netuid USMALLINT, reg_at UBIGINT, chain_price_rao UBIGINT, ts_price_rao UBIGINT, rel_diff DOUBLE);
"""  # noqa: E501 (section 7.1 verbatim: two DDL lines exceed 135 characters)

# ------------------------------------------------------------------------------------------------ section 7.2
JOURNAL_DDL: Final[str] = """\
PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;        -- backtests: ':memory:' or synchronous=OFF
CREATE TABLE journal (
  seq INTEGER PRIMARY KEY, batch INTEGER NOT NULL,         -- batch = seq of the first record of the atomic batch
  block INTEGER NOT NULL, phase INTEGER NOT NULL, sub INTEGER NOT NULL, book TEXT NOT NULL,
  kind TEXT NOT NULL, version INTEGER NOT NULL, payload BLOB NOT NULL,   -- canonical JSON (core.codec)
  idem TEXT UNIQUE,                                        -- idempotency key (NULL = not deduplicated)
  prev_hash BLOB NOT NULL, hash BLOB NOT NULL);            -- blake2b-256(prev || block|phase|sub|book|kind|version|payload)
CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER journal_no_delete BEFORE DELETE ON journal BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TABLE anchor (id INTEGER PRIMARY KEY CHECK (id = 1), head_seq INTEGER, head_hash BLOB);  -- updated in the same txn
"""

# ------------------------------------------------------------------------------------------------ section 7.3
STATE_DDL: Final[str] = """\
CREATE TABLE run_meta (run_id TEXT PRIMARY KEY, mode TEXT, code_hash TEXT, config_hash TEXT, prereg_hash TEXT,
  data_manifest_hash TEXT, parent_run TEXT, started_wall_ts INTEGER);
CREATE TABLE orders_proj (book TEXT, order_id TEXT, attempt INTEGER, state TEXT, kind TEXT, netuid INTEGER, reg_at INTEGER,
  hotkey TEXT, tao_in INTEGER, alpha_in INTEGER, limit_price INTEGER, urgency INTEGER, created_block INTEGER,
  terminal_block INTEGER, reason TEXT, PRIMARY KEY (book, order_id, attempt));
CREATE TABLE live_submissions (order_id TEXT, attempt INTEGER, delegate TEXT, nonce INTEGER, era_start INTEGER,
  era_end INTEGER, submit_head INTEGER, expected_fill_block INTEGER, used_nonce INTEGER,
  delegate_free_before INTEGER, carrier_hash TEXT, inner_hash TEXT, carrier_fee_settled INTEGER, state TEXT,
  PRIMARY KEY (order_id, attempt));                        -- written BEFORE any send; updated from the submit result
CREATE TABLE checkpoint (book TEXT, seq INTEGER, state_hash TEXT, blob_zstd BLOB, features_digest TEXT,
  PRIMARY KEY (book, seq));
CREATE TABLE fetch_ledger (block INTEGER PRIMARY KEY, status TEXT, attempts INTEGER, provider TEXT, last_error TEXT);
CREATE TABLE manifest (path TEXT PRIMARY KEY, tbl TEXT, first_block INTEGER, last_block INTEGER, rows INTEGER,
  sha256 TEXT, schema_version INTEGER, decoder_version INTEGER, created_wall_ts INTEGER);
CREATE TABLE endpoint_health (endpoint TEXT PRIMARY KEY, ewma_ms REAL, errors INTEGER, breaker TEXT, since INTEGER);
CREATE TABLE trial (trial_id TEXT PRIMARY KEY, cfg_hash TEXT, strategy TEXT, data_range TEXT, purpose TEXT, wall_ts INTEGER);
"""

SHARED_STATE_TABLES: Final[tuple[str, ...]] = ("fetch_ledger", "manifest", "endpoint_health")   # data/state.sqlite
RUN_STATE_TABLES: Final[tuple[str, ...]] = ("run_meta", "orders_proj", "live_submissions", "checkpoint", "trial")


def _strip_sql_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def create_table_statements(ddl: str) -> dict[str, str]:
    """name -> 'CREATE TABLE name (...)' for every table of a section-7 DDL block (comments removed)."""
    out: dict[str, str] = {}
    for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\);", _strip_sql_comments(ddl), flags=re.S):
        out[m.group(1)] = f"CREATE TABLE {m.group(1)} ({' '.join(m.group(2).split())})"
    return out


def if_not_exists(stmt: str) -> str:
    return stmt.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)


JOURNAL_PRAGMAS_DURABLE: Final[tuple[str, ...]] = ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL")
JOURNAL_SCHEMA_SQL: Final[str] = JOURNAL_DDL.split("\n", 1)[1]          # the DDL without its PRAGMA line
JOURNAL_TRIGGERS: Final[tuple[str, ...]] = ("journal_no_update", "journal_no_delete")


# ------------------------------------------------------------------------------------------------ lake column specs
INT_RANGES: Final[dict[str, tuple[int, int]]] = {
    "UTINYINT": (0, 2**8 - 1),
    "USMALLINT": (0, 2**16 - 1),
    "UINTEGER": (0, 2**32 - 1),
    "UBIGINT": (0, 2**64 - 1),
    "SMALLINT": (-(2**15), 2**15 - 1),
    "INTEGER": (-(2**31), 2**31 - 1),
    "BIGINT": (-(2**63), 2**63 - 1),
    "HUGEINT": (-(10**38) + 1, 10**38 - 1),      # stored as DECIMAL(38,0) in Parquet
}
OTHER_TYPES: Final[frozenset[str]] = frozenset({"VARCHAR", "BOOLEAN", "DOUBLE", "BLOB", "DATE"})


@dataclass(frozen=True, slots=True)
class Col:
    name: str
    type: str                  # DuckDB logical type as declared in section 7.1
    extra: bool = False        # added by WP3 (exact_json); not in section 7.1

    @property
    def storage_type(self) -> str:
        """Parquet-side type (HUGEINT has no Parquet equivalent: DECIMAL(38,0), cast back by the views)."""
        return "DECIMAL(38,0)" if self.type == "HUGEINT" else self.type

    def cast_from_text(self, sql_name: str) -> str:
        """SQL expression turning the to_sql_text() form of this column into its storage type. VARCHAR goes to
        DECIMAL(38,0) through HUGEINT: DuckDB 1.2's direct VARCHAR -> DECIMAL cast is ~300x slower."""
        if self.type == "BLOB":
            return f"unhex({sql_name})"
        if self.type == "HUGEINT":
            return f"CAST(CAST({sql_name} AS HUGEINT) AS DECIMAL(38,0))"
        return f"CAST({sql_name} AS {self.type})"

    @property
    def is_int(self) -> bool:
        return self.type in INT_RANGES


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    cols: tuple[Col, ...]
    key: tuple[str, ...]           # uniqueness key inside one chunk (section 7.1 PK); () = none
    sort: tuple[str, ...]          # chunk row order (every remaining column follows as a tie-break)
    block_col: str | None          # era partition and manifest first/last; None = unpartitioned dimension table
    view: str                      # DuckDB view name (Lake.connect)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.cols)

    def col(self, name: str) -> Col:
        if LAKE_TABLES.get(self.name) is self:            # the lake's own specs: dict lookup (hot path of row writing)
            c = _COL_INDEX[self.name].get(name)
            if c is not None:
                return c
        else:
            for c in self.cols:
                if c.name == name:
                    return c
        raise KeyError(f"{self.name}.{name}")


def _parse_lake_ddl(ddl: str) -> dict[str, list[Col]]:
    out: dict[str, list[Col]] = {}
    for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\);", _strip_sql_comments(ddl), flags=re.S):
        cols: list[Col] = []
        for part in m.group(2).split(","):
            words = part.split()
            if not words:
                continue
            typ = words[1]
            if typ not in INT_RANGES and typ not in OTHER_TYPES:
                raise ValueError(f"unsupported lake column type {typ} in {m.group(1)}")
            cols.append(Col(words[0], typ))
        out[m.group(1)] = cols
    return out


_EXACT_COL: Final[Col] = Col("exact_json", "VARCHAR", extra=True)
SNAPSHOT_TABLES: Final[tuple[str, ...]] = ("snap_global", "snap_subnet", "snap_hotkey")

_TABLE_META: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...], str | None, str]]] = {
    # name: (key, sort, block_col, view)
    "snap_global": (("block",), ("block",), "block", "v_global"),
    "snap_subnet": (("block", "netuid"), ("block", "netuid"), "block", "v_subnet"),
    "snap_hotkey": (("block", "netuid", "hotkey"), ("block", "netuid", "hotkey"), "block", "v_hotkey"),
    "dividend_keys": (("block", "netuid", "hotkey"), ("block", "netuid", "hotkey"), "block", "v_dividend_keys"),
    "generation": (("netuid", "reg_at"), ("netuid", "reg_at"), None, "v_generation"),
    "chain_event": ((), ("block", "kind", "netuid", "reg_at", "hotkey"), "block", "v_chain_event"),
    "registration": (("queued_block",), ("queued_block",), None, "v_registration"),
    "spec_boundary": (("spec_version",), ("spec_version",), None, "v_spec_boundary"),
    "calib": ((), ("block", "probe", "netuid"), "block", "v_calib"),
    "raw_rpc": ((), ("block", "call", "request_sha"), "block", "v_raw_rpc"),
    "ext_trades": ((), ("block", "netuid", "extrinsic_id", "seq"), "block", "v_ext_trades"),
    "ext_crosscheck": ((), ("day", "netuid", "reg_at"), None, "v_ext_crosscheck"),
}


def _build_tables() -> dict[str, TableSpec]:
    parsed = _parse_lake_ddl(LAKE_DDL)
    if sorted(parsed) != sorted(_TABLE_META):
        raise RuntimeError("LAKE_DDL tables and _TABLE_META disagree")
    out: dict[str, TableSpec] = {}
    for name, cols in parsed.items():
        key, sort, block_col, view = _TABLE_META[name]
        if name in SNAPSHOT_TABLES:
            cols = [*cols, _EXACT_COL]
        out[name] = TableSpec(name, tuple(cols), key, sort, block_col, view)
    return out


LAKE_TABLES: Final[dict[str, TableSpec]] = _build_tables()
_COL_INDEX: Final[dict[str, dict[str, Col]]] = {t: {c.name: c for c in spec.cols} for t, spec in LAKE_TABLES.items()}


def to_sql_text(col: Col, v: Any) -> str | None:
    """Validated text form of a Python value for `col` (DuckDB casts it back exactly). Raises on a bad value."""
    if v is None:
        return None
    t = col.type
    if t in INT_RANGES:
        if isinstance(v, bool) or not isinstance(v, int):
            raise TypeError(f"{col.name}: expected int, got {type(v).__name__}")
        lo, hi = INT_RANGES[t]
        if not lo <= v <= hi:
            raise ValueError(f"{col.name}: {v} outside {t}")
        return str(int(v))
    if t == "BOOLEAN":
        if not isinstance(v, bool):
            raise TypeError(f"{col.name}: expected bool, got {type(v).__name__}")
        return "true" if v else "false"
    if t == "VARCHAR":
        if not isinstance(v, str):
            raise TypeError(f"{col.name}: expected str, got {type(v).__name__}")
        return v
    if t == "DOUBLE":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError(f"{col.name}: expected float, got {type(v).__name__}")
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(f"{col.name}: non-finite DOUBLE {f!r}")
        return repr(f)
    if t == "BLOB":
        if not isinstance(v, (bytes, bytearray, memoryview)):
            raise TypeError(f"{col.name}: expected bytes, got {type(v).__name__}")
        return bytes(v).hex()
    if t == "DATE":
        if isinstance(v, _dt.datetime) or not isinstance(v, _dt.date):
            raise TypeError(f"{col.name}: expected datetime.date, got {type(v).__name__}")
        return v.isoformat()
    raise TypeError(f"{col.name}: unsupported type {t}")


def fits(col: Col, v: int) -> bool:
    lo, hi = INT_RANGES[col.type]
    return lo <= v <= hi


# ------------------------------------------------------------------------------------------------ era partition
# Directory layout only (data/lake/<table>/era=<A|B|C>/...). Pricing never branches on it: the pool kind stored per
# row (built by chain.reader, section 6.8 step 6) carries the era. Boundaries mirror section 8.2.
ERA_B_FIRST_BLOCK: Final[int] = 5_947_549
ERA_C_FIRST_BLOCK: Final[int] = 8_486_594


def era_of(block: int) -> str:
    if block >= ERA_C_FIRST_BLOCK:
        return "C"
    return "B" if block >= ERA_B_FIRST_BLOCK else "A"


# ------------------------------------------------------------------------------------------------ fixed point
FRAC_32: Final[int] = 32      # I96F32 / U96F32
FRAC_64: Final[int] = 64      # U64F64
TAO_WEIGHT_DEN: Final[int] = U64_MAX
MANTISSA_DIGITS: Final[int] = 38


# Magnitude guards: every value a section 7.1 raw column can hold has |value| < 10^40 and, for raw / 2^k, at most k
# fractional digits; TaoWeight raw / u64::MAX is <= 1 and either 0 or >= 5.4e-20. Anything outside these bounds goes
# straight to the exact_json fallback, so a pathological Decimal (e.g. 1E-999999999) never builds a huge integer.
_MAX_INT_DIGITS: Final[int] = 40


def _bounded(d: Decimal, max_frac_digits: int) -> tuple[int, int] | None:
    """(mantissa, exponent) of d if it is small enough to convert cheaply, else None."""
    if not d.is_finite():
        return None
    m, e = decimal_to_mant_exp(d)
    if m == 0:
        return 0, 0
    if e < -max_frac_digits or len(str(abs(m))) + e > _MAX_INT_DIGITS:
        return None
    return m, e


def fixed_to_raw(d: Decimal, frac_bits: int) -> int | None:
    """raw such that raw / 2^frac_bits == d exactly, else None."""
    me = _bounded(d, frac_bits)                  # 2^-k has exactly k fractional digits
    if me is None:
        return None
    m, e = me
    if e >= 0:
        return (m * int(10**e)) << frac_bits
    q, r = divmod(m << frac_bits, int(10**-e))
    return None if r else q


def fixed_floor_raw(d: Decimal, frac_bits: int) -> int | None:
    """floor(d * 2^frac_bits) for an analytics approximation; None if d is out of any column's range."""
    if not d.is_finite() or (d != 0 and d.adjusted() > _MAX_INT_DIGITS):
        return None
    return floor_int(EXACT.multiply(d, Decimal(1 << frac_bits)))


def raw_to_fixed(raw: int, frac_bits: int) -> Decimal:
    """Exact: 2^-k terminates, and |raw| < 2^128 needs at most ~90 of EXACT's 160 digits."""
    return EXACT.divide(Decimal(raw), Decimal(1 << frac_bits))


def ratio_to_raw(d: Decimal, den: int) -> int | None:
    """raw such that EXACT.divide(raw, den) == d (TaoWeight = raw / u64::MAX), else None."""
    if not d.is_finite() or d < 0 or (d != 0 and not -25 <= d.adjusted() <= 1):
        return None
    n, m = d.as_integer_ratio()                  # bounded: at most EXACT.prec digits, exponent >= about -185
    q, r = divmod(n * den, m)
    if 2 * r > m or (2 * r == m and q % 2):
        q += 1
    return q if raw_to_ratio(q, den) == d else None


def ratio_floor_raw(d: Decimal, den: int) -> int | None:
    """floor(d * den) for an analytics approximation; None if d is out of range."""
    if not d.is_finite() or (d != 0 and d.adjusted() > _MAX_INT_DIGITS):
        return None
    return floor_int(EXACT.multiply(d, Decimal(den)))


def raw_to_ratio(raw: int, den: int) -> Decimal:
    return EXACT.divide(Decimal(raw), Decimal(den))


def decimal_to_mant_exp(d: Decimal) -> tuple[int, int]:
    """Exact (mantissa, exponent) with the mantissa stripped of trailing zeros; zero -> (0, 0)."""
    sign, digits, exp = d.as_tuple()
    if not isinstance(exp, int):
        raise ValueError(f"non-finite Decimal {d!r}")
    ds = list(digits)
    while len(ds) > 1 and ds[-1] == 0:
        ds.pop()
        exp += 1
    m = int("".join(str(x) for x in ds))
    if m == 0:
        return 0, 0
    return (-m if sign else m), exp


def mant_exp_to_decimal(m: int, e: int) -> Decimal:
    return Decimal(f"{m}E{e}")


# ------------------------------------------------------------------------------------------------ snapshot digest
# A fast path for core.codec's canonical encoding of snapshots. core.codec re-derives the sorted field list of every
# dataclass instance it meets (about 600k calls per 128-subnet snapshot); this encoder caches the field list per class
# and hands every leaf it does not recognise (Decimal, Enum, float, bytes, mappings, sets) to core.codec.encode, so
# the result is byte-identical by construction for the dataclass/tuple/int/str/bool/None skeleton. tests/data/
# test_schema.py asserts fast_encode(x) == codec.encode(x) for the snapshot strategies and fixtures; a divergence
# would also make every lake write fail closed (with_digest compares against the producer's codec digest).
_FIELDS: dict[type[Any], tuple[str, ...]] = {}


def _field_names(cls: type[Any]) -> tuple[str, ...]:
    names = _FIELDS.get(cls)
    if names is None:
        names = tuple(sorted(f.name for f in dataclasses.fields(cls)
                             if not f.name.startswith("_") and f.metadata.get("codec", True) is not False))
        _FIELDS[cls] = names
    return names


def fast_encode(obj: Any) -> Any:
    """== core.codec.encode(obj) for dataclass trees (ChainSnapshot and its parts), several times faster."""
    t = type(obj)
    if t is int or t is str or t is bool or obj is None:
        return obj
    if t is tuple or t is list:
        return [fast_encode(x) for x in obj]
    names = _FIELDS.get(t)
    if names is None and dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        names = _field_names(t)
    if names is not None:
        return {n: fast_encode(getattr(obj, n)) for n in names}
    if t is Decimal:
        return _dec_text(obj)
    if isinstance(obj, Enum):
        return fast_encode(obj.value)
    return codec.encode(obj)


def _dec_text(d: Decimal) -> str:
    """core.codec's Decimal rule: exact format(d, "f"), trailing fractional zeros stripped, "-0" -> "0"."""
    if not d.is_finite():
        return str(codec.encode(d))                  # raises CodecError exactly like the codec
    txt = format(d, "f")
    if "." in txt:
        txt = txt.rstrip("0").rstrip(".")
    return "0" if txt in ("-0", "") else txt


def canonical_snapshot_bytes(snap: ChainSnapshot) -> bytes:
    """== core.codec.canonical_bytes(snap)."""
    return json.dumps(fast_encode(snap), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sealed_snapshot_bytes(snap: ChainSnapshot) -> tuple[ChainSnapshot, bytes]:
    """(snapshot with its digest verified or filled, its canonical bytes) from one encoding pass: what the hot
    staging writer needs. Raises ValueError like with_digest when an existing digest does not match."""
    enc = fast_encode(snap)
    enc["digest"] = ""
    d = hashlib.blake2b(json.dumps(enc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
                        digest_size=16).hexdigest()
    if snap.digest not in ("", d):
        raise ValueError(f"snapshot {snap.block}: digest {snap.digest} does not match its content ({d})")
    enc["digest"] = d
    sealed = snap if snap.digest == d else replace(snap, digest=d)
    return sealed, json.dumps(enc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def snapshot_digest(snap: ChainSnapshot) -> str:
    """blake2b-128 of the canonical bytes of the snapshot with digest="" (section 6.8 step 9);
    == core.codec.digest(dataclasses.replace(snap, digest=""))."""
    enc = fast_encode(snap)
    enc["digest"] = ""
    raw = json.dumps(enc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.blake2b(raw, digest_size=16).hexdigest()


def with_digest(snap: ChainSnapshot) -> ChainSnapshot:
    """The snapshot with its digest set; an existing digest must match (fail closed)."""
    d = snapshot_digest(snap)
    if snap.digest == "":
        return replace(snap, digest=d)
    if snap.digest != d:
        raise ValueError(f"snapshot {snap.block}: digest {snap.digest} does not match its content ({d})")
    return snap


def quality_or(snap: ChainSnapshot) -> int:
    q = 0
    for s in snap.subnets:
        q |= int(s.quality)
    return q


# ------------------------------------------------------------------------------------------------ snapshot -> rows
@dataclass(frozen=True, slots=True)
class SnapshotRows:
    glob: dict[str, Any]
    subnets: list[dict[str, Any]]
    hotkeys: list[dict[str, Any]]


class _W:
    """Row writer for one table: values that do not fit their column go to exact_json (lossless)."""
    __slots__ = ("ov", "row", "spec")

    def __init__(self, spec: TableSpec) -> None:
        self.spec = spec
        self.row: dict[str, Any] = {}
        self.ov: dict[str, str] = {}

    def int_(self, name: str, v: int | None) -> None:
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise TypeError(f"{self.spec.name}.{name}: expected int, got {type(v).__name__}")
        if v is None or fits(self.spec.col(name), v):
            self.row[name] = None if v is None else int(v)
        else:
            self.row[name] = None
            self.ov[name] = str(int(v))

    def val(self, name: str, v: Any) -> None:
        to_sql_text(self.spec.col(name), v)            # type check only
        self.row[name] = v

    def fixed(self, name: str, d: Decimal | None, frac_bits: int) -> None:
        if d is None:
            self.row[name] = None
            return
        raw = fixed_to_raw(d, frac_bits)
        if raw is not None and fits(self.spec.col(name), raw) and raw_to_fixed(raw, frac_bits) == d:
            self.row[name] = raw
            return
        approx = fixed_floor_raw(d, frac_bits)
        self.row[name] = approx if approx is not None and fits(self.spec.col(name), approx) else None
        self.ov[name] = codec.encode(d)

    def ratio(self, name: str, d: Decimal, den: int) -> None:
        raw = ratio_to_raw(d, den)
        if raw is not None and fits(self.spec.col(name), raw):
            self.row[name] = raw
            return
        approx = ratio_floor_raw(d, den)
        self.row[name] = approx if approx is not None and fits(self.spec.col(name), approx) else None
        self.ov[name] = codec.encode(d)

    def shares(self, m_name: str, e_name: str, d: Decimal) -> None:
        m, e = decimal_to_mant_exp(d)
        if fits(self.spec.col(m_name), m) and fits(self.spec.col(e_name), e):
            self.row[m_name], self.row[e_name] = m, e
            return
        digits = str(abs(m))
        drop = max(0, len(digits) - MANTISSA_DIGITS)
        am = int(digits[: len(digits) - drop]) * (-1 if m < 0 else 1)
        ae = e + drop
        ok = fits(self.spec.col(m_name), am) and fits(self.spec.col(e_name), ae)
        self.row[m_name], self.row[e_name] = (am, ae) if ok else (None, None)
        self.ov[m_name] = codec.encode(d)

    def done(self) -> dict[str, Any]:
        self.row["exact_json"] = json.dumps(self.ov, sort_keys=True, separators=(",", ":")) if self.ov else None
        missing = [n for n in self.spec.names if n not in self.row]
        if missing:
            raise RuntimeError(f"{self.spec.name}: row builder missed {missing}")
        return self.row


def snapshot_to_rows(snap: ChainSnapshot, *, decoder_version: int = DEFAULT_DECODER_VERSION,
                     escrow_block: Mapping[int, int] | None = None,
                     shares_src: Mapping[tuple[int, str], int] | None = None) -> SnapshotRows:
    """Lake rows of one snapshot (digest filled/verified). escrow_block (netuid -> forward-fill source block) and
    shares_src ((netuid, hotkey) -> 1 V1 / 2 V2) are informational columns the snapshot does not carry."""
    snap = with_digest(snap)
    netuids = [int(s.key.netuid) for s in snap.subnets]
    if netuids != sorted(set(netuids)):
        raise ValueError(f"snapshot {snap.block}: subnets must be sorted by netuid and unique")
    g = snap.glob
    w = _W(LAKE_TABLES["snap_global"])
    w.int_("block", int(snap.block))
    w.val("block_hash", str(snap.block_hash))
    w.int_("ts_ms", snap.timestamp_ms)
    w.int_("plan", int(snap.plan))
    w.int_("spec_version", g.spec_version)
    w.int_("tx_version", g.tx_version)
    w.int_("total_issuance", int(g.total_issuance))
    w.int_("block_emission", int(g.block_emission))
    w.fixed("moving_alpha_raw", g.moving_alpha, FRAC_32)
    w.fixed("gate_bar_raw", g.gate_bar, FRAC_64)
    w.int_("gate_rank", g.gate_rank)
    w.int_("gate_exponent", g.gate_exponent)
    w.ratio("tao_weight_raw", g.tao_weight, TAO_WEIGHT_DEN)
    w.int_("root_tao", int(g.root_tao))
    w.int_("owner_cut_u16", g.owner_cut_u16)
    w.int_("subnet_limit", g.subnet_limit)
    w.int_("immunity_period", g.immunity_period)
    w.int_("network_rate_limit", g.network_rate_limit)
    w.int_("last_reg_block", int(g.last_reg_block))
    w.int_("last_lock_cost", int(g.last_lock_cost))
    w.int_("min_lock_cost", int(g.min_lock_cost))
    w.int_("lock_reduction_interval", g.lock_reduction_interval)
    w.int_("tao_in_refund_block", int(g.tao_in_refund_block))
    w.int_("nominator_min_stake", int(g.nominator_min_stake))
    w.int_("cleanup_queue_len", g.cleanup_queue_len)
    w.int_("n_nonroot_networks", g.n_nonroot_networks)
    w.int_("safe_mode_until", None if g.safe_mode_until is None else int(g.safe_mode_until))
    w.val("shorts_enabled", g.shorts_enabled)
    w.int_("runtime_prune_target", None if g.runtime_prune_target is None else int(g.runtime_prune_target))
    w.val("digest", snap.digest)
    w.int_("quality_or", quality_or(snap))
    w.int_("decoder_version", decoder_version)
    glob_row = w.done()

    subnet_rows: list[dict[str, Any]] = []
    hotkey_rows: list[dict[str, Any]] = []
    for s in snap.subnets:
        subnet_rows.append(_subnet_row(snap.block, s, decoder_version, escrow_block))
        hks = [str(h.hotkey) for h in s.hotkeys]
        if hks != sorted(set(hks)):
            raise ValueError(f"snapshot {snap.block} netuid {s.key.netuid}: hotkeys must be sorted and unique")
        for h in s.hotkeys:
            hw = _W(LAKE_TABLES["snap_hotkey"])
            hw.int_("block", int(snap.block))
            hw.int_("netuid", int(s.key.netuid))
            hw.int_("reg_at", int(s.key.reg_at))
            hw.val("hotkey", str(h.hotkey))
            hw.int_("total_alpha", int(h.total_alpha))
            src = None if shares_src is None else shares_src.get((int(s.key.netuid), str(h.hotkey)))
            hw.int_("shares_src", src)
            hw.shares("shares_mantissa", "shares_exp", h.total_shares)
            hw.int_("take_u16", h.take_u16)
            hw.int_("childkey_take_u16", h.childkey_take_u16)
            hw.val("earns", h.earns)
            hw.int_("last_dividend", int(h.last_dividend))
            hotkey_rows.append(hw.done())
    return SnapshotRows(glob_row, subnet_rows, hotkey_rows)


def _subnet_row(block: int, s: SubnetState, decoder_version: int, escrow_block: Mapping[int, int] | None) -> dict[str, Any]:
    p = s.pool
    w = _W(LAKE_TABLES["snap_subnet"])
    w.int_("block", int(block))
    w.int_("netuid", int(s.key.netuid))
    w.int_("reg_at", int(s.key.reg_at))
    w.int_("pool_kind", int(p.kind))
    w.int_("tao", int(p.tao))
    w.int_("alpha_in", int(p.alpha))
    w.int_("px_tao", p.px_tao)
    w.int_("px_alpha", p.px_alpha)
    w.int_("w_quote_e18", p.w_quote_e18)
    w.int_("fee_rate", p.fee_rate)
    w.int_("reservoir_tao", int(s.reservoir_tao))
    w.int_("reservoir_alpha", int(s.reservoir_alpha))
    w.int_("alpha_out", int(s.alpha_out))
    w.int_("protocol_alpha", int(s.protocol_alpha))
    w.fixed("moving_price_raw", s.moving_price, FRAC_32)
    w.fixed("fast_moving_raw", s.fast_moving_price, FRAC_64)
    w.fixed("root_prop_raw", s.root_prop, FRAC_32)
    w.fixed("miner_burned_raw", s.miner_burned, FRAC_32)
    w.val("emission_enabled", s.emission_enabled)
    w.val("subtoken_enabled", s.subtoken_enabled)
    w.val("reg_allowed", s.reg_allowed)
    w.int_("first_emission_block", None if s.first_emission_block is None else int(s.first_emission_block))
    w.int_("tempo", s.tempo)
    w.int_("last_epoch_block", int(s.last_epoch_block))
    w.int_("ema_halving_blocks", s.ema_halving_blocks)
    w.int_("tao_in_emission", int(s.tao_in_emission))
    w.int_("excess_tao", int(s.excess_tao))
    w.int_("alpha_out_emission", int(s.alpha_out_emission))
    w.int_("alpha_in_emission", int(s.alpha_in_emission))
    w.int_("tao_flow_cum", s.tao_flow_cum)
    w.int_("volume_cum", s.volume_cum)
    w.val("owner_coldkey", None if s.owner_coldkey is None else str(s.owner_coldkey))
    w.val("owner_hotkey", None if s.owner_hotkey is None else str(s.owner_hotkey))
    w.val("owner_cut_enabled", s.owner_cut_enabled)
    w.val("owner_cut_autolock", s.owner_cut_autolock)
    w.int_("total_alpha_staked", None if s.total_alpha_staked is None else int(s.total_alpha_staked))
    w.int_("max_allowed_validators", s.max_allowed_validators)
    w.int_("consensus_mode", s.consensus_mode)
    mg = s.metagraph
    w.int_("mg_n_miners", None if mg is None else mg.n_miners)
    w.int_("mg_n_miner_coldkeys", None if mg is None else mg.n_miner_coldkeys)
    w.int_("mg_top1_coldkey_ppm", None if mg is None else mg.top1_coldkey_share_ppm)
    w.int_("mg_n_permit_coldkeys", None if mg is None else mg.n_permit_coldkeys)
    w.int_("escrow_alpha", None if s.escrow_alpha is None else int(s.escrow_alpha))
    w.int_("escrow_block", None if escrow_block is None else escrow_block.get(int(s.key.netuid)))
    w.int_("owner_alpha", None if s.owner_alpha is None else int(s.owner_alpha))
    w.int_("quality", int(s.quality))
    w.int_("decoder_version", decoder_version)
    return w.done()


# ------------------------------------------------------------------------------------------------ rows -> snapshot
class _R:
    """Row reader: applies exact_json overrides; a NULL in a required field is a DecodeError."""
    __slots__ = ("ov", "row", "where")

    def __init__(self, row: Mapping[str, Any], where: str) -> None:
        self.row = row
        self.where = where
        raw = row.get("exact_json")
        try:
            ov = json.loads(raw) if raw else {}
        except ValueError as e:
            raise DecodeError(f"{where}: bad exact_json") from e
        if not isinstance(ov, dict) or not all(isinstance(v, str) for v in ov.values()):
            raise DecodeError(f"{where}: bad exact_json")
        self.ov: dict[str, str] = ov

    def opt_int(self, name: str) -> int | None:
        if name in self.ov:
            try:
                return int(self.ov[name])
            except ValueError as e:
                raise DecodeError(f"{self.where}.{name}: bad exact text") from e
        v = self.row.get(name)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
            raise DecodeError(f"{self.where}.{name}: expected int, got {type(v).__name__}")
        return int(v)

    def int_(self, name: str) -> int:
        v = self.opt_int(name)
        if v is None:
            raise DecodeError(f"{self.where}.{name}: NULL in a required column")
        return v

    def opt_val(self, name: str, typ: type[Any]) -> Any:
        v = self.row.get(name)
        if v is not None and (not isinstance(v, typ) or (typ is not bool and isinstance(v, bool))):
            raise DecodeError(f"{self.where}.{name}: expected {typ.__name__}, got {type(v).__name__}")
        return v

    def val(self, name: str, typ: type[Any]) -> Any:
        v = self.opt_val(name, typ)
        if v is None:
            raise DecodeError(f"{self.where}.{name}: NULL in a required column")
        return v

    def _exact_dec(self, name: str) -> Decimal:
        try:
            d = Decimal(self.ov[name])
        except ArithmeticError as e:
            raise DecodeError(f"{self.where}.{name}: bad exact text") from e
        if not d.is_finite():
            raise DecodeError(f"{self.where}.{name}: non-finite exact text")
        return d

    def opt_fixed(self, name: str, frac_bits: int) -> Decimal | None:
        if name in self.ov:
            return self._exact_dec(name)
        raw = self.opt_int(name)
        return None if raw is None else raw_to_fixed(raw, frac_bits)

    def fixed(self, name: str, frac_bits: int) -> Decimal:
        d = self.opt_fixed(name, frac_bits)
        if d is None:
            raise DecodeError(f"{self.where}.{name}: NULL in a required column")
        return d

    def ratio(self, name: str, den: int) -> Decimal:
        if name in self.ov:
            return self._exact_dec(name)
        return raw_to_ratio(self.int_(name), den)

    def shares(self, m_name: str, e_name: str) -> Decimal:
        if m_name in self.ov:
            return self._exact_dec(m_name)
        return mant_exp_to_decimal(self.int_(m_name), self.int_(e_name))


def _enum(cls: Any, v: int, where: str) -> Any:
    try:
        return cls(v)
    except ValueError as e:
        raise DecodeError(f"{where}: {v} is not a valid {cls.__name__}") from e


def snapshot_from_rows(glob_row: Mapping[str, Any], subnet_rows: Sequence[Mapping[str, Any]],
                       hotkey_rows: Sequence[Mapping[str, Any]], *, verify: bool = True) -> ChainSnapshot:
    """Inverse of snapshot_to_rows. verify=True recomputes the digest and requires it to equal the stored one."""
    r = _R(glob_row, "snap_global")
    block = r.int_("block")
    where = f"snapshot {block}"
    glob = ChainGlobals(
        spec_version=r.int_("spec_version"), tx_version=r.int_("tx_version"),
        total_issuance=Rao(r.int_("total_issuance")), block_emission=Rao(r.int_("block_emission")),
        moving_alpha=r.fixed("moving_alpha_raw", FRAC_32), gate_bar=r.fixed("gate_bar_raw", FRAC_64),
        gate_rank=r.int_("gate_rank"), gate_exponent=r.int_("gate_exponent"),
        tao_weight=r.ratio("tao_weight_raw", TAO_WEIGHT_DEN), root_tao=Rao(r.int_("root_tao")),
        owner_cut_u16=r.int_("owner_cut_u16"), subnet_limit=r.int_("subnet_limit"),
        immunity_period=r.int_("immunity_period"), network_rate_limit=r.int_("network_rate_limit"),
        last_reg_block=Block(r.int_("last_reg_block")), last_lock_cost=Rao(r.int_("last_lock_cost")),
        min_lock_cost=Rao(r.int_("min_lock_cost")), lock_reduction_interval=r.int_("lock_reduction_interval"),
        tao_in_refund_block=Block(r.int_("tao_in_refund_block")), nominator_min_stake=Rao(r.int_("nominator_min_stake")),
        cleanup_queue_len=r.int_("cleanup_queue_len"), n_nonroot_networks=r.int_("n_nonroot_networks"),
        safe_mode_until=_opt_block(r.opt_int("safe_mode_until")), shorts_enabled=r.val("shorts_enabled", bool),
        runtime_prune_target=_opt_netuid(r.opt_int("runtime_prune_target")),
    )
    by_netuid: dict[int, list[Mapping[str, Any]]] = {}
    for h in hotkey_rows:
        if h.get("block") != block:
            raise DecodeError(f"{where}: hotkey row of block {h.get('block')}")
        by_netuid.setdefault(int(h["netuid"]), []).append(h)
    subnets: list[SubnetState] = []
    for row in sorted(subnet_rows, key=lambda x: int(x["netuid"])):
        if row.get("block") != block:
            raise DecodeError(f"{where}: subnet row of block {row.get('block')}")
        hk = sorted(by_netuid.pop(int(row["netuid"]), []), key=lambda x: str(x["hotkey"]))
        fast = _subnet_fast(row, hk)
        subnets.append(fast if fast is not None else _subnet_from_row(row, hk, where))
    if by_netuid:
        raise DecodeError(f"{where}: hotkey rows for netuids without a subnet row {sorted(by_netuid)}")
    snap = ChainSnapshot(block=Block(block), block_hash=BlockHash(r.val("block_hash", str)), timestamp_ms=r.int_("ts_ms"),
                         plan=_enum(ReadPlan, r.int_("plan"), where), glob=glob, subnets=tuple(subnets),
                         digest=r.val("digest", str))
    if verify:
        d = snapshot_digest(snap)
        if d != snap.digest:
            raise DecodeError(f"{where}: stored digest {snap.digest} != recomputed {d}")
    return snap


# Fast path of _subnet_from_row for the common case (no exact_json override, no NULL in a required column, typed
# values as DuckDB returns them from the chunk's declared column types). Anything unusual returns None and the
# validating slow path decodes the row (or raises the precise DecodeError). Equivalence is property-tested.
_TWO_32: Final[Decimal] = Decimal(1 << FRAC_32)
_TWO_64: Final[Decimal] = Decimal(1 << FRAC_64)
_SUBNET_REQUIRED: Final[tuple[str, ...]] = (
    "netuid", "reg_at", "pool_kind", "tao", "alpha_in", "px_tao", "px_alpha", "w_quote_e18", "fee_rate", "reservoir_tao",
    "reservoir_alpha", "alpha_out", "protocol_alpha", "moving_price_raw", "root_prop_raw", "miner_burned_raw", "tempo",
    "last_epoch_block", "ema_halving_blocks", "tao_in_emission", "excess_tao", "alpha_out_emission", "alpha_in_emission",
    "quality")
_HOTKEY_REQUIRED: Final[tuple[str, ...]] = (
    "hotkey", "total_alpha", "shares_mantissa", "shares_exp", "take_u16", "childkey_take_u16", "earns", "last_dividend")
_MG_COLS: Final[tuple[str, ...]] = ("mg_n_miners", "mg_n_miner_coldkeys", "mg_top1_coldkey_ppm", "mg_n_permit_coldkeys")
_get_subnet_required = itemgetter(*_SUBNET_REQUIRED)
_get_hotkey_required = itemgetter(*_HOTKEY_REQUIRED)
_get_mg = itemgetter(*_MG_COLS)


def _subnet_fast(row: Mapping[str, Any], hotkey_rows: Sequence[Mapping[str, Any]]) -> SubnetState | None:
    try:
        if row["exact_json"] is not None or None in _get_subnet_required(row):
            return None
        flags = (row["emission_enabled"], row["subtoken_enabled"], row["reg_allowed"])
        if type(flags[0]) is not bool or type(flags[1]) is not bool or type(flags[2]) is not bool:
            return None
        reg_at = row["reg_at"]
        hotkeys: list[HotkeyIdx] = []
        for h in hotkey_rows:
            if h["exact_json"] is not None or h["reg_at"] != reg_at or None in _get_hotkey_required(h):
                return None
            if type(h["earns"]) is not bool:
                return None
            hotkeys.append(HotkeyIdx(hotkey=Hotkey(h["hotkey"]), total_alpha=AlphaRao(h["total_alpha"]),
                                     total_shares=Decimal(f"{h['shares_mantissa']}E{h['shares_exp']}"),
                                     take_u16=h["take_u16"], childkey_take_u16=h["childkey_take_u16"], earns=h["earns"],
                                     last_dividend=AlphaRao(h["last_dividend"])))
        mg_vals = _get_mg(row)
        if all(v is None for v in mg_vals):
            mg = None
        elif any(v is None for v in mg_vals):
            return None
        else:
            mg = MetagraphLite(mg_vals[0], mg_vals[1], mg_vals[2], mg_vals[3])
        pool = PoolState(kind=PoolKind(row["pool_kind"]), tao=Rao(row["tao"]), alpha=AlphaRao(row["alpha_in"]),
                         px_tao=row["px_tao"], px_alpha=row["px_alpha"], w_quote_e18=row["w_quote_e18"],
                         fee_rate=row["fee_rate"])
        fmp = row["fast_moving_raw"]
        feb = row["first_emission_block"]
        tas = row["total_alpha_staked"]
        esc = row["escrow_alpha"]
        own = row["owner_alpha"]
        ock = row["owner_coldkey"]
        ohk = row["owner_hotkey"]
        return SubnetState(
            key=SubnetKey(NetUid(row["netuid"]), Block(reg_at)), pool=pool, alpha_out=AlphaRao(row["alpha_out"]),
            protocol_alpha=AlphaRao(row["protocol_alpha"]),
            moving_price=EXACT.divide(Decimal(row["moving_price_raw"]), _TWO_32),
            root_prop=EXACT.divide(Decimal(row["root_prop_raw"]), _TWO_32),
            miner_burned=EXACT.divide(Decimal(row["miner_burned_raw"]), _TWO_32),
            emission_enabled=flags[0], subtoken_enabled=flags[1], reg_allowed=flags[2],
            first_emission_block=None if feb is None else Block(feb), tempo=row["tempo"],
            last_epoch_block=Block(row["last_epoch_block"]), ema_halving_blocks=row["ema_halving_blocks"],
            tao_in_emission=Rao(row["tao_in_emission"]), excess_tao=Rao(row["excess_tao"]),
            alpha_out_emission=AlphaRao(row["alpha_out_emission"]), alpha_in_emission=AlphaRao(row["alpha_in_emission"]),
            reservoir_tao=Rao(row["reservoir_tao"]), reservoir_alpha=AlphaRao(row["reservoir_alpha"]),
            tao_flow_cum=row["tao_flow_cum"], volume_cum=row["volume_cum"],
            fast_moving_price=None if fmp is None else EXACT.divide(Decimal(fmp), _TWO_64),
            owner_coldkey=None if ock is None else Coldkey(ock), owner_hotkey=None if ohk is None else Hotkey(ohk),
            owner_cut_enabled=row["owner_cut_enabled"], owner_cut_autolock=row["owner_cut_autolock"],
            total_alpha_staked=None if tas is None else AlphaRao(tas), escrow_alpha=None if esc is None else AlphaRao(esc),
            owner_alpha=None if own is None else AlphaRao(own), max_allowed_validators=row["max_allowed_validators"],
            consensus_mode=row["consensus_mode"], metagraph=mg, hotkeys=tuple(hotkeys), quality=Quality(row["quality"]),
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def _opt_block(v: int | None) -> Block | None:
    return None if v is None else Block(v)


def _opt_netuid(v: int | None) -> NetUid | None:
    return None if v is None else NetUid(v)


def _subnet_from_row(row: Mapping[str, Any], hotkey_rows: Sequence[Mapping[str, Any]], where: str) -> SubnetState:
    r = _R(row, f"{where} snap_subnet")
    netuid, reg_at = r.int_("netuid"), r.int_("reg_at")
    w = f"{where} netuid {netuid}"
    pool = PoolState(kind=_enum(PoolKind, r.int_("pool_kind"), w), tao=Rao(r.int_("tao")), alpha=AlphaRao(r.int_("alpha_in")),
                     px_tao=r.int_("px_tao"), px_alpha=r.int_("px_alpha"), w_quote_e18=r.int_("w_quote_e18"),
                     fee_rate=r.int_("fee_rate"))
    mg_vals = [r.opt_int(n) for n in ("mg_n_miners", "mg_n_miner_coldkeys", "mg_top1_coldkey_ppm", "mg_n_permit_coldkeys")]
    if all(v is None for v in mg_vals):
        mg = None
    elif any(v is None for v in mg_vals):
        raise DecodeError(f"{w}: partial metagraph columns")
    else:
        a, b, c, d = (int(v) for v in mg_vals if v is not None)
        mg = MetagraphLite(n_miners=a, n_miner_coldkeys=b, top1_coldkey_share_ppm=c, n_permit_coldkeys=d)
    hotkeys: list[HotkeyIdx] = []
    for h in hotkey_rows:
        hr = _R(h, f"{w} snap_hotkey")
        if hr.int_("reg_at") != reg_at:
            raise DecodeError(f"{w}: hotkey row reg_at {hr.int_('reg_at')} != {reg_at}")
        hotkeys.append(HotkeyIdx(hotkey=Hotkey(hr.val("hotkey", str)), total_alpha=AlphaRao(hr.int_("total_alpha")),
                                 total_shares=hr.shares("shares_mantissa", "shares_exp"), take_u16=hr.int_("take_u16"),
                                 childkey_take_u16=hr.int_("childkey_take_u16"), earns=hr.val("earns", bool),
                                 last_dividend=AlphaRao(hr.int_("last_dividend"))))
    feb = r.opt_int("first_emission_block")
    tas = r.opt_int("total_alpha_staked")
    esc = r.opt_int("escrow_alpha")
    own = r.opt_int("owner_alpha")
    ock = r.opt_val("owner_coldkey", str)
    ohk = r.opt_val("owner_hotkey", str)
    return SubnetState(
        key=SubnetKey(NetUid(netuid), Block(reg_at)), pool=pool, alpha_out=AlphaRao(r.int_("alpha_out")),
        protocol_alpha=AlphaRao(r.int_("protocol_alpha")), moving_price=r.fixed("moving_price_raw", FRAC_32),
        root_prop=r.fixed("root_prop_raw", FRAC_32), miner_burned=r.fixed("miner_burned_raw", FRAC_32),
        emission_enabled=r.val("emission_enabled", bool), subtoken_enabled=r.val("subtoken_enabled", bool),
        reg_allowed=r.val("reg_allowed", bool), first_emission_block=None if feb is None else Block(feb),
        tempo=r.int_("tempo"), last_epoch_block=Block(r.int_("last_epoch_block")),
        ema_halving_blocks=r.int_("ema_halving_blocks"), tao_in_emission=Rao(r.int_("tao_in_emission")),
        excess_tao=Rao(r.int_("excess_tao")), alpha_out_emission=AlphaRao(r.int_("alpha_out_emission")),
        alpha_in_emission=AlphaRao(r.int_("alpha_in_emission")), reservoir_tao=Rao(r.int_("reservoir_tao")),
        reservoir_alpha=AlphaRao(r.int_("reservoir_alpha")), tao_flow_cum=r.opt_int("tao_flow_cum"),
        volume_cum=r.opt_int("volume_cum"), fast_moving_price=r.opt_fixed("fast_moving_raw", FRAC_64),
        owner_coldkey=None if ock is None else Coldkey(ock), owner_hotkey=None if ohk is None else Hotkey(ohk),
        owner_cut_enabled=r.opt_val("owner_cut_enabled", bool), owner_cut_autolock=r.opt_val("owner_cut_autolock", bool),
        total_alpha_staked=None if tas is None else AlphaRao(tas), escrow_alpha=None if esc is None else AlphaRao(esc),
        owner_alpha=None if own is None else AlphaRao(own), max_allowed_validators=r.opt_int("max_allowed_validators"),
        consensus_mode=r.opt_int("consensus_mode"), metagraph=mg, hotkeys=tuple(hotkeys),
        quality=Quality(r.int_("quality")),
    )
