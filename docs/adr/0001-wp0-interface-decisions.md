# ADR-0001: Lead decisions on WP0's interface requests

**Status:** accepted (lead), 2026-10-09
**Context:** While building WP0 (core types, tooling and golden fixtures), the builder raised six requests. Each one is decided here and binds every WP.

## Decisions

1. **SN1 1-TAO vector.** The design figure `alpha_out 152,285,961,807` could not be reproduced. The canonical vector is block **9,240,388**: spot 6,562,800 rao/alpha, net 999,496,453, fee 503,547, alpha_out **152,290,647,774** rao (`tests/fixtures/golden/sn1_quote_9240388.json`). DESIGN §10.1 is corrected.

2. **`EmissionGateExponent` is U64F64 on chain** (specs 441 and 475; default raw = 3.0). `ChainGlobals.gate_exponent` stays `int`, so the §5 types are unchanged.
   - WP1 decodes U64F64.
   - If the value is integral, WP1 returns it as an int.
   - If it is non-integral, WP1 raises `DecodeError`, which fails closed: the snapshot quality is degraded and emission parity is not trusted. A future runtime that sets a fractional h then needs a new ADR to widen the type.

3. **`ShortsEnabled` does not exist** in the metadata at specs 348, 441 or 475.
   - WP1 does not register the item.
   - `ChainGlobals.shorts_enabled` keeps its default `False`.
   - If the item appears in future metadata, `verify-metadata` should flag it so it can be added deliberately.

4. **Codec §5.11 extensions are accepted as canonical:**
   - a Mapping with non-str keys is encoded as an array of `[key, value]` pairs, sorted by the canonical text of the encoded key;
   - a str-keyed mapping stays a JSON object;
   - a Decimal is encoded as a JSON string, and `-0` is written `0`.

5. **Static rule wording.** "No `execute(` outside live/" means no **SDK executor** call. `sqlite3.Connection.execute` is allowed. The AST gate enforces the `bittensor` import ban and the `UnstakeAll` / `TransferStake` / `Batch` identifier bans. DESIGN §4.2 is reworded.

6. **Ignore files.** `.import_linter_cache/` and `.hypothesis/` are added to `.gitignore`. Data ignores are anchored at the repo root (`/data/`), because an unanchored `data/` would have ignored the `src/taotrader/data/` package.

## Also binding (WP0 deviations accepted as-is)
- **Ledger helper signatures:** `fill_txn`, `yield_txn(ev)`, `dereg_txn(ev, pos_alpha=None)`, `fail_txn`, `carrier_fee_txn`, `capital_txn`, `apply_txn`. `POSITION_TOLERANCE_RAO = 2`.
- **Config env prefix:** `TAOTRADER_CFG_`, with `__` as the path separator. The TOML string `"none"` means None. A `[book_defaults]` table is merged under each book.
- **Line length:** ruff line-length is 135.
- **Venv:** sync it with `uv sync --frozen --all-extras` (dev is an extra). Only the lead runs uv.
