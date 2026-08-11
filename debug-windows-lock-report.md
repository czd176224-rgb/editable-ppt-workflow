# Windows V6 mutation lock debugging report

## Scope

Investigate the intermittent Windows failure in
`test_project_429_throttle_limits_subsequent_page_launches_and_preserves_completed`
without changing scheduler behavior, suppressing unrelated permission failures,
or weakening project-path containment.

## Evidence gathered before editing production code

- The focused failing test passed 20 consecutive isolated runs, confirming that
  the original suite failure is timing-dependent.
- `workflow_v6_state.mutation_lock()` uses an ephemeral directory as the lock:
  acquisition calls `Path.mkdir()`, while release calls `Path.rmdir()`.
- Acquisition retries only `FileExistsError`. The observed failure is
  `PermissionError: [WinError 5] Access is denied` at `lock.mkdir()`.
- A raw Windows filesystem stress reproduction with eight threads repeatedly
  creating and removing the same lock directory produced 2,999 WinError 5
  acquisition failures in eight seconds, alongside normal `FileExistsError`
  contention. The parent temporary directory remained writable.
- The repository's working cross-thread/process lock pattern in `cache_store.py`
  keeps a regular project-local lock file and acquires a one-byte OS advisory
  lock (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere), so it does not
  create/remove the lock pathname at every handoff.
- Recent Task 7/8 work increased the number of short mutations sharing the same
  project lock (scheduler leases, page ownership, request ledger, receipts),
  making the pre-existing directory handoff race substantially easier to hit.

## Single root-cause hypothesis (recorded before the fix)

The failure is caused by the ephemeral directory-lock lifecycle itself: on
Windows, a contender calling `CreateDirectory` while another thread's
`RemoveDirectory` is still completing can receive `ERROR_ACCESS_DENIED`
(WinError 5) instead of `ERROR_ALREADY_EXISTS`. Because `mutation_lock()` only
classifies `FileExistsError` as contention, a normal handoff race escapes as a
fatal permission error. Replacing the create/remove lifecycle with a persistent,
validated project-local lock file plus an OS byte-range lock should remove that
race without treating arbitrary permission failures as contention.

## TDD and verification record

### RED

Three production-behavior regressions were added before the fix:

1. The lock pathname remains one regular file with the same identity across
   sequential handoffs.
2. A pre-existing directory at the lock pathname is rejected as invalid rather
   than treated as ordinary contention.
3. Eight threads complete 2,000 real acquisitions with mutual exclusion and no
   handoff exceptions.

All three failed against the directory implementation. The stress test captured
five real WinError 5 failures in that run.

### Minimal fix

`workflow_v6_state.mutation_lock()` now:

- keeps `.workflow_v6.lock` as a persistent regular file;
- rejects symlinks, reparse points, hard links, directories, and paths whose
  resolved identity is not the literal project-local file;
- verifies the open descriptor and pathname identify the same file before and
  after acquiring the lock;
- uses `msvcrt.locking(..., LK_NBLCK, 1)` on Windows and non-blocking
  `fcntl.flock` on POSIX;
- retries only advisory-lock contention error codes. Permission failures while
  opening or validating the pathname are not reclassified or suppressed.

No scheduler, receipt, Image2, prompt, or page-state behavior was changed.

### GREEN and stress evidence

- New lock regressions: `3 passed`.
- New 8-thread/2,000-acquisition stress test repeated 20 times: `20 passed`.
- Original 429 concurrent-page test repeated 20 times: `20 passed`.
- Combined Task 7/8 focused suites (`workflow_v6_image`, V6 concurrency, style
  contract, generation receipt boundary, request ledger, generation trace):
  `210 passed in 71.51s`.

Final syntax and diff checks are recorded in the commit handoff.
