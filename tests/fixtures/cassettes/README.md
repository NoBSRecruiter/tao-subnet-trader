# Chain reader cassettes (WP1)

Recorded JSON-RPC sessions for offline reader tests (`tests/chain/test_cassette.py`). Each `.jsonl` line is one
exchange `{"method", "params", "result"}`; `CassetteTransport` replays them by canonical (method, params), and a request
that is not in the cassette is a fatal error, never a network call.

| File | Content |
|---|---|
| `full_9240388.jsonl` | FULL snapshot at 9,240,388 (`0xa57ba6d8...9408524`, spec 475): runtime version, the SN92 dividend-key listing, the storage reads (globals, 144 netuids, SN92 tracked hotkeys, owner positions), block emission and prune-target runtime calls |
| `head_9240389.jsonl` | the HEAD snapshot of the next block (built on the FULL one) |
| `reader_9240388.json` | expected digests and a few decoded values written at recording time |
| `header_9240388.json` | `chain_getHeader` at 9,240,388 (checks `chain.head.header_hash`) |

Regenerate (read-only, public OnFinality archive, <= 2.5 req/s, about 5 s):

    .venv/Scripts/python.exe tests/chain/record_cassettes.py

Re-record whenever the reader's request shape changes (registry rows, chunking, read plans); the replay test then fails
with cassette misses. The replay test cross-checks decoded values against WP0's golden fixtures captured independently at
the same block hash, so a recording made by a faulty reader cannot pass.
