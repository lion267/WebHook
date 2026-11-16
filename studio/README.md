# Studio tracker (Phase 2)

Stdlib Python 3 only. DB: `studio/studio.db` (override with `STUDIO_DB`).

    python3 studio/tools/studio.py job-create 20261116-01-slug "Product" --url URL
    python3 studio/tools/studio.py task-claim 20261116-01-slug:concept   # exit 3 if not claimable
    python3 studio/tools/studio.py task-finish 20261116-01-slug:concept completed
    python3 studio/tools/studio.py approve  20261116-01-slug product approved
    python3 studio/tools/studio.py milestone 20261116-01-slug video_verified   # checks MP4 via ffprobe
    python3 studio/tools/studio.py job-complete 20261116-01-slug               # needs all 7 milestones + 1080x1920 + audio
    python3 studio/tools/studio.py recover      # after restart: stale running tasks -> pending/failed
    python3 studio/tools/studio.py serve --port 8787

## Read-only API (127.0.0.1 only; used by studio/dashboard/index.html)
`GET /api/overview` · `/api/jobs` · `/api/jobs/<id>` · `/api/events?after=<id>` · `/api/stream` (Server-Sent Events, resumes with `Last-Event-ID`).
Event `source`: `hook` (observed by Claude Code hooks), `manager` (Production Manager actions), `agent`.

## Hooks (.claude/settings.json)
PreToolUse/PostToolUse/SubagentStart/SubagentStop -> `studio.py hook` (never blocks Claude). Also denies all `mcp__meta_fb_ads` tools.
STATUS: verified 2026-11-16 in a real Claude Code session. Observed fields: PreToolUse/PostToolUse = hook_event_name, tool_name, tool_input, tool_use_id, cwd, session_id, transcript_path, permission_mode, prompt_id, effort (+ tool_response, duration_ms on Post); subagent tool calls add agent_id + agent_type; SubagentStart/Stop = hook_event_name, agent_id, agent_type, cwd. Project hooks were hot-loaded mid-session, but project agents in .claude/agents/ are only registered when a session starts.

Tests: `python3 -I studio/tests/test_studio.py` (uses a temp DB).

## Dashboard (no install, no build)
    python3 studio/tools/studio.py serve        # then open http://127.0.0.1:8787
Shows daily tiles, per-agent status, videos with milestone progress, task/approval/asset detail, event timeline, and a live feed (SSE).
Everything shown comes from the DB; an empty DB shows an empty dashboard. Rendered and checked in headless Chromium on 2026-11-16 (SSE connected, no JS errors).
