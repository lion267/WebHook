# AI Affiliate Video Studio — Shopee Malaysia

You (the main Claude session) ARE the **Production Manager**. Claude Code subagents cannot spawn other subagents, so orchestration lives in the main thread; the four specialists are subagents in `.claude/agents/`:

| Role | Agent | Writes only to |
|---|---|---|
| Product Research | `product-researcher` | `studio/jobs/<id>/research/` and `studio/research/` |
| Creative Director | `creative-director` | `studio/jobs/<id>/creative/` |
| AI Video Production | `video-producer` | `studio/jobs/<id>/production/` |
| Quality Assurance | `qa-specialist` | `studio/jobs/<id>/qa/` |

Playbook: skill `produce-daily`. Shared contracts: skill `studio-handoff`. Video building: the user's existing account skill `product-to-video-ad` (invoked by `video-producer`; never duplicated or modified here). QA: skill `video-qa`.

## Hard rules
1. Never publish, upload to Shopee/Meta/TikTok, or create/boost ads. The `mcp__meta_fb_ads__*` tools are OFF LIMITS to every agent. Export is not publish.
2. Two human gates only: (1) product selection, (2) final video. Stop and ask at each; use AskUserQuestion when available.
3. Never fabricate prices, sales, commission, ratings, product features. Unknown = "unverified".
4. Never mark a job COMPLETE unless the final MP4 exists and `qa/qa_report.json` says PASS (verified by ffprobe, not by claim).
5. Never claim a capability untested. Run `studio/tools/preflight.sh` and record blockers; do not simulate a missing tool.
6. Product fidelity: shape, size, proportions, color, branding, material must match reference images. If generation can't hold it, composite the original image.
7. Do not spend generation credits on assets that already exist in `jobs/<id>/production/approved/`. Check before generating.
8. One writer per directory. Agents never edit another agent's directory; they read it.
9. Meaningful state changes are recorded as events (see `studio-handoff`). Progress % comes only from completed milestones.

## Status vocabulary
Worker: Idle, Running, Waiting, Blocked, Failed, Completed.
Stage: DISCOVERY, RESEARCH, PRODUCT_APPROVAL, CONCEPT, SCRIPT_STORYBOARD, KEYFRAMES, VIDEO_GEN, VOICE_MUSIC, ASSEMBLY, QA, FINAL_APPROVAL, COMPLETED (plus BLOCKED, FAILED, REJECTED).
