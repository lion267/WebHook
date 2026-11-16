---
name: produce-daily
description: Production Manager playbook for "Produce N affiliate videos today". Runs the full pipeline - research, product approval, creative, video build, QA, final approval - delegating to the four specialist subagents with dependency tracking, retries, and reporting.
---

# Produce N affiliate videos today

You are the Production Manager (main thread). Delegate; do not do specialist work yourself.

## 0. Preflight
1. Run `bash studio/tools/preflight.sh`; summarize blockers to the user (missing TTS / image-gen / video-gen / dotnet etc.).
2. Read `studio/history/` and `studio/jobs/*/job.json`. Resume unfinished jobs for today before creating new ones; never duplicate a product or an in-flight render.
3. Target N (3-5). Quality beats volume: if QA keeps failing, produce fewer and say so.

## 1. Discovery + research (parallel)
Spawn `product-researcher` (N+3 candidates, with `exclude` list). While waiting, nothing else is needed.

## 2. Gate 1 — product approval (HUMAN)
Present ranked candidates: why promising, evidence + source, angle, what is unverified. Use AskUserQuestion (multiSelect) for approve/reject. Do not proceed on unapproved products. Record decisions in each `job.json`.

## 3. Create jobs
For each approved product: `video_id = YYYYMMDD-NN-<slug>`, create `studio/jobs/<id>/` per `studio-handoff` layout, write `job.json` (stage `CONCEPT`), copy reference images to `research/images/`. Append event `product_approved`.

## 4. Pipeline per job (jobs run independently / in parallel)
CONCEPT+SCRIPT_STORYBOARD → `creative-director`
VIDEO PRODUCTION → `video-producer`, which invokes the existing `product-to-video-ad` skill once with the research + creative inputs (opaque step: record start, finish, duration, output; no invented sub-milestones). If it reports the skill unavailable, run that skill yourself in the main thread.
QA → `qa-specialist` (fresh context, independent)
- QA FAIL: route per `route_to`; max 3 revision loops per job, then mark `NEEDS_HUMAN`.
- Launch up to 3 specialist agents concurrently (rate limits); send multiple Agent calls in a single message. Different jobs may overlap (Creative on Video 2 while QA checks Video 1; Researcher for tomorrow while renders run).
- Dependencies: a stage starts only when the previous stage's handoff file exists and validates. Never run two agents against one job directory at once.
- After each agent returns: verify the files it claims exist (ls / ffprobe). Don't trust summaries. Update `job.json`, append events.
- Failures: transient → retry (max 2, log). Tool missing → `BLOCKED` with the exact blocker. Never fake output.

## 5. Gate 2 — final approval (HUMAN)
Only for jobs with QA PASS and an existing MP4. Send the file with SendUserFile plus QA summary; ask approve / reject / revise. Revision → back to the stage the notes concern.
Only after approval set `COMPLETED`. Do NOT publish anywhere.

## 6. Daily report
Write `studio/history/<date>/report.md`: target vs completed, in production, blocked/failed with reasons, awaiting approval, per-video stage + QA verdict, assets spent, blockers needing the user's action.

## Tracking (persistent, source of truth)
Use `python3 studio/tools/studio.py ...` (see studio/README.md): `job-create`, `task-claim <id>:<concept|produce|qa>` (exit 3 = not claimable: duplicate, deps unmet, or max attempts), `task-finish`, `milestone`, `approve`, `job-complete`, `recover` (run at session start after any restart), `overview`. `milestone video_verified/qa_pass` and `job-complete` check the MP4 and QA report on disk and refuse otherwise; never bypass them by editing the DB.

Also:
Keep a TaskCreate/TaskUpdate list mirroring jobs/stages. Persistent truth is the files in `studio/jobs/` (and the SQLite DB once Phase 2 lands), not chat context.
