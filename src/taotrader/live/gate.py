"""taotrader/live/gate.py - the four-lock gate, the HMAC arm token and the UNARMED state (WP11; DESIGN.md 9.3).

| Lock | Requirement |
|---|---|
| 1 config | [live] enabled = true |
| 2 config confirmation | [live] mode = "submit"; `network` set explicitly in a live config file (not only by the
|                       | defaults in config/default.toml); spec_version in accepted_specs; `sleeves` lists only
|                       | LIVE_ELIGIBLE sleeves |
| 3 CLI | `taotrader live --live --submit` (both flags) |
| 4 environment | TAOTRADER_LIVE_ARMED = "<expiry_unix>:<hmac>", hmac = HMAC-SHA256(arm secret from the OS keyring,
|               | config_hash || expiry), expiry <= 24 h ahead, compared with hmac.compare_digest. Any config edit
|               | changes config_hash and so invalidates the token. Mainnet also needs TAOTRADER_LIVE_NETWORK_CONFIRM=finney |
| plan-only (live_dry) | lock 1 + `--live` only: real intents, client.plan(), journaled, never submitted |

Any failure raises core.errors.GateError listing every missing lock (exit non-zero).

UNARMED (section 9.3): an authentic token for the CURRENT config hash that has expired, or a spec_version not in
accepted_specs, admits a start only when LiveCfg.risk_exits_when_unarmed is true (a systemd crash restart keeps the
risk exits); otherwise the gate refuses. At runtime `Arming.state()` re-evaluates expiry (injected clock) and the spec on
every submission: UNARMED makes LiveVenue refuse everything except, with the flag, EMERGENCY/URGENT full sells while
V2/V3/V6 pass. With the flag off, `ArmingMonitor` alerts at T - 2 h before expiry and on every spec change while ladder
exposure (held positions with prune_rank <= 15) is above 0, listing each position with its rank and t*.

The wall clock enters only here (token expiry), never a decision: the Engine stays a pure function of the journal.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets as _stdlib_secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final

from ..core.config import LiveCfg, RiskCfg, RunCfg
from ..core.errors import GateError
from ..core.fixed import DEC
from ..core.state import ChainSnapshot
from ..core.units import PPM, Stage, StrategyId, SubnetKey
from ..ops import config_load
from ..ops.secrets import require_secret
from ..protocol.prune import prune_rank, time_to_target
from .sdk_port import ss58_decode

__all__ = [
    "ARM_ENV", "ARM_SECRET_NAME", "CONFIRM_ENV", "EXPIRY_ALERT_S", "LADDER_EXPOSURE_RANK", "MAX_ARM_TTL_S", "Arming",
    "ArmingMonitor", "GateDecision", "LiveState", "TokenStatus", "arm_mac", "check_arm_token", "evaluate_gate",
    "explicit_live_keys", "ladder_exposure", "load_arm_secret", "make_arm_token", "new_arm_secret",
]

log = logging.getLogger("taotrader.live.gate")

ARM_ENV: Final[str] = "TAOTRADER_LIVE_ARMED"
CONFIRM_ENV: Final[str] = "TAOTRADER_LIVE_NETWORK_CONFIRM"
ARM_SECRET_NAME: Final[str] = "live_arm_hmac"          # ops.secrets.KNOWN: keyring only, no env fallback
MAX_ARM_TTL_S: Final[int] = 24 * 3600
EXPIRY_ALERT_S: Final[int] = 2 * 3600                   # alert at T - 2 h (risk_exits_when_unarmed = false)
LADDER_EXPOSURE_RANK: Final[int] = 15                   # ladder bucket: positions with prune_rank <= 15
TSTAR_HORIZON_BLOCKS: Final[int] = 21_600               # t* reported in unarmed alerts (3 d horizon)
MIN_SECRET_BYTES: Final[int] = 16


class LiveState(StrEnum):
    PLAN_ONLY = "plan_only"      # live-dry: plan() only, never submits
    ARMED = "armed"              # all four locks: normal submission
    UNARMED = "unarmed"          # token expired or spec not accepted: nothing, or (flag) EMERGENCY/URGENT full sells


class TokenStatus(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"          # authentic for this config hash, expiry passed
    MISSING = "missing"
    MALFORMED = "malformed"
    FORGED = "forged"            # MAC mismatch: another secret or another config hash (a config edit)
    TOO_LONG = "too_long"        # expiry more than 24 h ahead
    NO_SECRET = "no_secret"


# ------------------------------------------------------------------------------------------------- arm token
def arm_mac(secret: bytes, config_hash: str, expiry_unix: int) -> str:
    """HMAC-SHA256(secret, config_hash || expiry) as lowercase hex. config_hash is fixed-width hex, so plain
    concatenation is unambiguous."""
    return hmac.new(secret, f"{config_hash}{expiry_unix}".encode("ascii"), hashlib.sha256).hexdigest()


def make_arm_token(secret: bytes, config_hash: str, expiry_unix: int, *, now_unix: int) -> str:
    """The TAOTRADER_LIVE_ARMED value ("<expiry_unix>:<hmac>") created by the user-run `taotrader live arm`."""
    if len(secret) < MIN_SECRET_BYTES:
        raise GateError("the arm secret is too short")
    if not now_unix < expiry_unix <= now_unix + MAX_ARM_TTL_S:
        raise GateError(f"arm token expiry must be in (now, now + {MAX_ARM_TTL_S} s]")
    return f"{expiry_unix}:{arm_mac(secret, config_hash, expiry_unix)}"


def check_arm_token(token: str | None, secret: bytes | None, config_hash: str, now_unix: int) -> tuple[TokenStatus, int | None]:
    """(status, expiry). The MAC is checked with hmac.compare_digest BEFORE the expiry, so EXPIRED means authentic."""
    if not token:
        return TokenStatus.MISSING, None
    if not secret:
        return TokenStatus.NO_SECRET, None
    exp_text, sep, mac = token.strip().partition(":")
    if not sep or not exp_text.isdigit() or len(mac) != 64:
        return TokenStatus.MALFORMED, None
    expiry = int(exp_text)
    if not hmac.compare_digest(mac.lower(), arm_mac(secret, config_hash, expiry)):
        return TokenStatus.FORGED, None
    if expiry > now_unix + MAX_ARM_TTL_S:
        return TokenStatus.TOO_LONG, expiry
    if expiry <= now_unix:
        return TokenStatus.EXPIRED, expiry
    return TokenStatus.VALID, expiry


def new_arm_secret() -> str:
    """A fresh arm-token HMAC key (64 hex). `taotrader live arm --init-secret` stores it with ops.secrets.set_secret."""
    return _stdlib_secrets.token_hex(32)


def load_arm_secret() -> bytes:
    """The arm-token HMAC key from the live host's OS keyring (ops.secrets "live_arm_hmac"; never env, argv or logs)."""
    return require_secret(ARM_SECRET_NAME).reveal().encode("utf-8")


# ------------------------------------------------------------------------------------------------- config helpers
def explicit_live_keys(paths: Sequence[str | Path], env: Mapping[str, str] | None = None,
                       cli: Sequence[str] = ()) -> frozenset[str]:
    """[live] keys set explicitly by the user: in a config file other than config/default.toml, a TAOTRADER_CFG_LIVE__*
    variable or a `live.<key>=` CLI override. Lock 2 needs `network` among them."""
    keys: set[str] = set()
    default = config_load.DEFAULT_CONFIG.resolve()
    for p in paths:
        if Path(p).resolve() == default:
            continue
        live = config_load.read_toml(p).get("live")
        if isinstance(live, Mapping):
            keys.update(str(k) for k in live)
    for path, _ in config_load.env_overrides(env or {}):
        if len(path) == 2 and path[0] == "live":
            keys.add(path[1])
    for path, _ in config_load.cli_overrides(cli):
        if len(path) == 2 and path[0] == "live":
            keys.add(path[1])
    return frozenset(keys)


def _sleeve_problems(cfg: RunCfg) -> list[str]:
    stages: dict[StrategyId, set[Stage]] = {}
    for b in cfg.books:
        for s in b.sleeves:
            stages.setdefault(s.strategy, set()).add(s.stage)
    out: list[str] = []
    for sid in cfg.live.sleeves:
        got = stages.get(sid)
        if not got:
            out.append(f"lock 2: live.sleeves lists {sid!r}, which no book defines")
        elif got != {Stage.LIVE_ELIGIBLE}:
            out.append(f"lock 2: live.sleeves lists {sid!r}, which is not LIVE_ELIGIBLE in every book "
                       f"({sorted(s.name for s in got)})")
    return out


def _identity_problems(live: LiveCfg) -> list[str]:
    out: list[str] = []
    try:
        real = ss58_decode(live.real_coldkey_ss58)
    except ValueError:
        out.append("lock 2: [live] real_coldkey_ss58 is not a valid SS58 address")
        real = ""
    if not live.delegate_wallets:
        out.append("lock 2: [live] delegate_wallets must name the Staking-proxy delegate wallets (2-3)")
    elif len(set(live.delegate_wallets)) != len(live.delegate_wallets):
        out.append("lock 2: [live] delegate_wallets has duplicates")
    elif real and live.real_coldkey_ss58 in live.delegate_wallets:
        out.append("lock 2: a delegate may never be the real coldkey")
    return out


# ------------------------------------------------------------------------------------------------- the gate
@dataclass(frozen=True, slots=True)
class GateDecision:
    state: LiveState
    reasons: tuple[str, ...]              # why UNARMED (empty when ARMED or PLAN_ONLY)
    token_expiry: int | None              # unix seconds (None in plan-only)
    network: str
    config_hash: str

    @property
    def submit(self) -> bool:
        return self.state is not LiveState.PLAN_ONLY


def evaluate_gate(cfg: RunCfg, *, config_hash: str, cli_live: bool, cli_submit: bool, env: Mapping[str, str],
                  secret: bytes | None, now_unix: int, spec_version: int, explicit_keys: frozenset[str]) -> GateDecision:
    """Evaluate the four locks (section 9.3). Returns PLAN_ONLY, ARMED or UNARMED; raises GateError otherwise."""
    lv = cfg.live
    fails: list[str] = []
    if not lv.enabled:
        fails.append("lock 1 (config): [live] enabled = true is required")
    if not cli_live:
        fails.append("lock 3 (CLI): --live is required")
    if fails:
        raise GateError("; ".join(fails))
    if not cli_submit:
        return GateDecision(LiveState.PLAN_ONLY, (), None, lv.network, config_hash)
    if lv.mode != "submit":
        fails.append('lock 2 (config confirmation): [live] mode = "submit" is required')
    if "network" not in explicit_keys:
        fails.append("lock 2 (config confirmation): [live] network must be set explicitly in the live config")
    fails += _sleeve_problems(cfg)
    fails += _identity_problems(lv)
    if lv.network == "finney" and env.get(CONFIRM_ENV) != "finney":
        fails.append(f"lock 4 (environment): mainnet needs {CONFIRM_ENV}=finney")
    status, expiry = check_arm_token(env.get(ARM_ENV), secret, config_hash, now_unix)
    if status not in (TokenStatus.VALID, TokenStatus.EXPIRED):
        fails.append(f"lock 4 (environment): {ARM_ENV} is {status.value} (create it with `taotrader live arm`; any config "
                     "edit invalidates it)")
    if fails:
        raise GateError("; ".join(fails))
    unarmed: list[str] = []
    if status is TokenStatus.EXPIRED:
        unarmed.append("arm token expired")
    if spec_version not in lv.accepted_specs:
        unarmed.append(f"spec_version {spec_version} not in accepted_specs")
    if unarmed:
        if not lv.risk_exits_when_unarmed:
            raise GateError("; ".join(unarmed) + ": an UNARMED start needs risk_exits_when_unarmed = true (re-arm instead)")
        log.warning("live starts UNARMED (%s): only EMERGENCY/URGENT full sells while V2/V3/V6 pass", "; ".join(unarmed))
        return GateDecision(LiveState.UNARMED, tuple(unarmed), expiry, lv.network, config_hash)
    return GateDecision(LiveState.ARMED, (), expiry, lv.network, config_hash)


class Arming:
    """Runtime arming state for LiveVenue: re-evaluated on every submission (token expiry via the injected clock, and
    the current spec_version against accepted_specs). Re-arming means a restart with a new token (the process reads
    its environment once)."""

    def __init__(self, decision: GateDecision, live: LiveCfg, clock: Callable[[], int]) -> None:
        self.decision = decision
        self.live = live
        self.clock = clock

    @property
    def plan_only(self) -> bool:
        return self.decision.state is LiveState.PLAN_ONLY

    def state(self, spec_version: int) -> LiveState:
        if self.plan_only:
            return LiveState.PLAN_ONLY
        exp = self.decision.token_expiry
        if exp is None or self.clock() >= exp or spec_version not in self.live.accepted_specs:
            return LiveState.UNARMED
        return LiveState.ARMED

    def seconds_to_expiry(self) -> int | None:
        exp = self.decision.token_expiry
        return None if exp is None or self.plan_only else exp - self.clock()


# ------------------------------------------------------------------------------------------------- unarmed alerts
def ladder_exposure(snap: ChainSnapshot, held: Sequence[SubnetKey], risk: RiskCfg) -> list[tuple[SubnetKey, int, int | None]]:
    """Held generations with prune_rank <= 15, with their stressed time-to-target t* (blocks; None = beyond 3 d)."""
    out: list[tuple[SubnetKey, int, int | None]] = []
    for key in sorted(set(held)):
        rank = prune_rank(snap, key)
        s = snap.get(key)
        if rank is None or rank > LADDER_EXPOSURE_RANK or s is None:
            continue
        tstar: int | None = None
        if s.pool.px_tao > 0 and s.pool.px_alpha > 0:
            stressed = DEC.multiply(s.pool.spot(), DEC.divide(Decimal(PPM - risk.d_stress_ppm), Decimal(PPM)))
            tstar = time_to_target(snap, key, stressed, TSTAR_HORIZON_BLOCKS)
        out.append((key, rank, tstar))
    return out


class ArmingMonitor:
    """With risk_exits_when_unarmed = false: alert at T - 2 h before the arm token expires and on every spec change
    while ladder exposure > 0 (section 9.3). Called by the reconciler each tick; `on_alert(kind, message)`."""

    def __init__(self, arming: Arming, risk: RiskCfg, on_alert: Callable[[str, str], None] | None = None) -> None:
        self.arming = arming
        self.risk = risk
        self.on_alert = on_alert
        self._expiry_alerted: int | None = None
        self._last_spec: tuple[int, int] | None = None

    def check(self, snap: ChainSnapshot, held: Sequence[SubnetKey]) -> list[str]:
        spec = (snap.glob.spec_version, snap.glob.tx_version)
        changed = self._last_spec is not None and spec != self._last_spec
        self._last_spec = spec
        if self.arming.plan_only or self.arming.live.risk_exits_when_unarmed:
            return []
        exposure = ladder_exposure(snap, held, self.risk)
        if not exposure:
            return []
        listing = ", ".join(f"SN{int(k.netuid)} (reg {int(k.reg_at)}) rank {r} t*={'>3d' if t is None else f'{t} blocks'}"
                            for k, r, t in exposure)
        msgs: list[str] = []
        ttl = self.arming.seconds_to_expiry()
        exp = self.arming.decision.token_expiry
        if ttl is not None and 0 <= ttl <= EXPIRY_ALERT_S and self._expiry_alerted != exp:
            self._expiry_alerted = exp
            msgs.append(f"arm token expires in {ttl // 60} min with ladder exposure; risk_exits_when_unarmed is off, so no "
                        f"exit can be submitted until you re-arm: {listing}")
        if changed:
            msgs.append(f"spec changed to {spec[0]} (tx {spec[1]}) with ladder exposure; live is UNARMED until the spec is "
                        f"accepted and you re-arm, and no exit can be submitted meanwhile: {listing}")
        for m in msgs:
            log.warning("live_unarmed: %s", m)
            if self.on_alert is not None:
                self.on_alert("live_unarmed", m)
        return msgs
