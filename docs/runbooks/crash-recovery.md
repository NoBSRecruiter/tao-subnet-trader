# Runbook: crash recovery

**Covers:** `taotrader replay --recover`, ReplayDivergence handling, and `--accept-drift`. Design references: DESIGN.md sections 4.5 (crash windows), 4.6 (determinism) and 7.2 (journal).

## What a crash can and cannot do
- Every input, and every decision derived from it, commits in **one** SQLite transaction (WAL, `synchronous=FULL`). Side effects happen only after the commit. A crash therefore leaves either the whole batch or nothing.
- Orders are idempotent. An order caught in `SUBMITTING` is journaled as `SubmitUnknown(recovered_submitting)` and resolved from chain truth. It is **never re-sent blindly**.
- Recovery runs automatically at every start of `paper` and `live`. You normally only need to start the process again (Task Scheduler / systemd already do this).

## 1. Normal restart (paper, Windows)
1. Check that nothing is running: `scripts\run.cmd doctor`. A `run ... (running)` line means the lock is held. A second instance always exits **4**.
2. Start it: `scripts\run.cmd paper --config config\books.paper.toml`. Alternatively, let `tao-paper` restart it.
3. Confirm the heartbeat: `data\runs\paper-main\status.json` should show a rising `block`, `ticks`, and `journal.seq`.

## 2. Check integrity without touching the run
- `scripts\run.cmd verify-journal --all` verifies the hash chain of every journal. It also checks the heartbeat head: the journal must still contain the `status.json` head, so a whole-file rollback is detected.
- `scripts\run.cmd replay --config config\books.paper.toml` re-runs recovery on a **copy** of the journal, in VERIFY mode, from the recorded snapshots. Nothing is written.
  - Exit 0 means every journaled decision was reproduced byte for byte. This is also the nightly paper<->offline replay-equality check.
  - Exit 5 means `ReplayDivergence` (section 4 below).
- `--use-checkpoints` starts the copy from the newest usable checkpoint, which is faster on long runs.

## 3. Recover in place
`scripts\run.cmd replay --recover --config config\books.paper.toml`

This takes the run's lock, so stop the process first. It then runs the full recovery on the real journal and exits:
1. verify the chain;
2. replay and verify the decisions;
3. journal `SubmitUnknown` for every SUBMITTING order;
4. drain, resolve UNKNOWN orders, and re-drive the outbox.

Use it to see the recovery result before you start trading again. For live, run it on the Linux host with `--config config/live.toml`.

## 4. ReplayDivergence (exit 5)
The journal no longer matches what the current code and config decide. **Do not delete the journal.**
1. Read the message.
   - If it says *"written under ConfigApplied (...), the current run is (...)"*, the code, config or preregistration changed since the journal was written. This is expected after an upgrade or a config edit.
   - Any other divergence, on unchanged code and config, is a **bug** (a determinism break). Keep the journal and the lake, and report it with the log `logs/taotrader-<command>.jsonl`.
2. After an intended change, resume with `--accept-drift`: `scripts\run.cmd paper --config config\books.paper.toml --accept-drift`. It journals a new `ConfigApplied`. VERIFY is then off for the earlier batches, which are folded rather than re-decided. Later batches are verified under the new hashes.
3. Live: any config edit also invalidates the arm token, so re-arm afterwards (`live-arming.md`).

## 5. Journal damaged (verify-journal: INTEGRITY FAILURE)
1. Stop the process. Keep the damaged file, renamed for example `journal.sqlite.damaged`.
2. Restore the newest nightly `VACUUM INTO` copy from `<BackupRoot>\<date>\data\runs\<run>\journal.sqlite`.
3. Run `verify-journal`, then `replay` (on a copy).
4. Start with `--accept-drift` only if the restored copy predates a code or config change.
5. The restored run resumes after its last journaled block. The feed fills the gap, and events span it. Paper fills in the lost window are gone; they are research data, not money.
6. Live: reconciliation (chain truth) then journals `ReconAdjusted` for any difference, and entries halt until you run `clear-quarantine` (`key-compromise.md`, step 5).

## 6. Disk full / poisoned runner
A failed commit poisons the Runner, and the process exits rather than continuing in an unknown state. Free space, then restart. Nothing partial was written.
