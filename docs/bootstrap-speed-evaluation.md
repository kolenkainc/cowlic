# Bootstrap speed: measurements and decision (#262)

Research date: 2026-10-17. Keep one worker, N=1. The top-30 bootstrap has finished; a new production ingest solely to obtain a benchmark is not justified. No ingest code, dependency, account, machine or pacing changed. No second YouTube account, cookies, proxies, PO tokens or client rotation are used.

## What the available material actually measures

The private local `~/.local/state/cowlic/bootstrap.log` has 18 completed `run` sessions and no per-phase timing records. The last ten completed sessions contain the following aggregates, with artist, album and track names omitted:

| Session in this sample | Wall seconds | Successful additions | Logged track errors |
| --- | ---: | ---: | ---: |
| 1 | 5036 | 267 | 32 |
| 2 | 7786 | 447 | 53 |
| 3 | 740 | 42 | 3 |
| 4 | 1350 | 72 | 8 |
| 5 | 4585 | 272 | 28 |
| 6 | 5990 | 367 | 33 |
| 7 | 5895 | 361 | 39 |
| 8 | 6013 | 351 | 35 |
| 9 | 583 | 30 | 4 |
| 10 | 1 | 0 | 0 |

The nine nonempty sessions total **37978 seconds, 2209 successful additions and 235 logged track errors**: 17.19 wall seconds per successful addition, including enumeration and all other session overhead. Errors divided by additions plus errors is 9.6%; this is neither the HTTP 403 rate nor an attempt-level failure rate. The historic 14–17 seconds per attempt and 15% HTTP 403 in #262 refer to another sample and denominator.

Reproduction: pair each timestamped wrapper `start: run` with its `finished` timestamp, count successful `ingested:` and track `ERROR` lines (the `ERROR ` prefix and ` - ` delimiter) only inside that session, discard incomplete sessions, take the last ten, then sum durations and counts. Private names are not required for this calculation. The one-second zero-addition session is excluded from the throughput denominator. This is an observational sample without experimental controls, not a throughput guarantee.

| Phase requested in #262 | Measured duration |
| --- | --- |
| Last.fm enumeration | Unavailable: no start/end timing pair |
| YouTube Music search | Unavailable |
| yt-dlp download | Unavailable |
| ffmpeg | Unavailable |
| Tagging | Unavailable |
| Cover fetch | Unavailable |
| Telegram upload | Unavailable |
| D1 write | Unavailable |

The existing source shows serial work and pacing, but source inspection cannot supply missing durations. A meaningful next measurement needs monotonic phase boundaries on the next normal bounded ingest, a cold/warm enumeration distinction, attempt outcomes and actual HTTP statuses. Record counts and durations only, preserving the sign-in stop and existing cooldowns. That instrumentation is a separate owner decision because this ticket forbids code changes. No percentage of time is guessed here.

## Options at the same scope

These are bounds and hypotheses, not measured speedups. For parallel fraction p, an ideal N-worker speedup is `1 / ((1-p) + p/N)` before shared pacing and contention. p is unknown. N=2 therefore has a ceiling of 2x, with no established positive gain and possible regression.

| Option | Expected gain supported by this evidence | Cost and risk | Recommendation |
| --- | --- | --- | --- |
| Keep N=1 | Baseline observed above | Existing local lock, state, metrics and stop rules | Keep now |
| In-process N=2 downloads/uploads | Unmeasured; ideal ceiling 2x, less with serial pacing | Shared Last.fm/search pacing must be global; increased same-IP pressure; pending work must stop on sign-in | Profile first; no implementation yet |
| Second Mac, disjoint artist ranks | Unmeasured; ideal two-worker ceiling 2x before aggregate limits | Shared public IP; local flock/state do not coordinate machines; duplicate uploads can race before D1 dedupe | No-go now |
| Airflow, Docker or Docker Swarm | No measured throughput improvement from orchestration alone | Scheduling, queue/retries and UI add services; macOS VM, networking, secrets, locks, timer and log integration need maintenance | No-go for this workload |
| Cache Last.fm enumeration for one day | Historic estimate about two minutes saved per resumed artist; not timed here | Invalidation, ranking changes and missed fresh tracks | Measure repeated enumeration first |
| Larger sessions | Startup overhead amortized; magnitude unknown | Longer lock hold, timer overlap guard and caps remain | Already available; use existing caps |
| Faster no_match handling | No measured gain | Cooldown semantics already skip previous results; less searching can reduce match quality | Measure before changing |
| Skip live/demo/remix releases | Reduces volume, not demonstrated throughput | Changes what music is collected | Requires a separate library policy decision |

A second account offers nothing: the downloader is unauthenticated. Two Macs at home share the same public IP and aggregate upstream limits. Artist partitioning prevents intended overlap, but startup D1 dedupe is a snapshot: two workers can both upload the same candidate before either writes its row, even if the final ID upsert deduplicates metadata. Machine-local `ingest.lock`, ranking and progress files give no distributed exclusion.

Before any second-machine experiment, audit installation of the pinned Python environment, ffmpeg and deno, provide private local configuration, and replace Linux-only wrapper assumptions deliberately: macOS has no default flock or systemd (launchd is separate). `scripts/sync-secrets.sh` does not exist on master and is not an installation step. Do not copy secrets through the repository. Nothing was run on either Mac.

## Owner checkpoint and limits

Accept a bounded no-go now, or authorize a small timing-only follow-up on the next legitimate ingest before considering N=2. A future experiment should compare matched N=1/N=2 batches from one input partition with global pacing, stop immediately on sign-in, and stop/revert on increased HTTP 403 frequency against its measured baseline, duplicate uploads, broken progress, or timer interference. Do not set a fabricated numeric 403 threshold from the unrelated historical sample.

The original per-phase measurement criterion remains **unmet**. Keep #262 open pending that choice and a real profile if selected. No expected duration for the already-finished bootstrap is promised.
