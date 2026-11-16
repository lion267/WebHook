---
name: studio-handoff
description: Shared contracts for the affiliate video studio - job directory layout, job.json schema, event log format, and the structured handoff formats between researcher, creative director, video producer and QA. Read before writing any handoff file.
---

# Studio handoff contracts

## Layout
```
studio/
  research/<date>/candidates.json|md
  history/<date>/report.md
  jobs/<video_id>/
    job.json                # Manager-owned
    events.jsonl            # append-only, any agent may append one line per event
    research/   product_brief.json, images/      # product-researcher
    creative/   concept.md, script.json, storyboard.json, captions.json   # creative-director
    production/ keyframes/ clips/ audio/ approved/ final/ production_log.json  # video-producer
    qa/         qa_report.json|md, frames/       # qa-specialist
  tools/        preflight.sh (+ DB tooling in Phase 2)
```
Write atomically: write `name.tmp`, then `mv` to final name. Never edit another role's directory.

## job.json (Manager)
`{video_id, product_name, product_url, stage, status, assigned_worker, milestones:{research,product_approved,creative_ready,skill_invoked,video_verified,qa_pass,final_approved: true|false}, revision_loops, blockers:[], final_path, approval:{product, final}, created_at, updated_at}`
Progress % = completed milestones / 7. Only observable milestones; the product-to-video-ad skill is one opaque step (skill_invoked -> video_verified via ffprobe).

## events.jsonl line
`{"ts":"ISO-8601 +08:00","video_id":"...","agent":"...","event":"keyframe_generated","detail":"...","artifact":"relative/path|null"}`
Standard events: research_started, research_completed, product_approved, script_generated, creative_ready, skill_invoked, skill_finished, video_assembled (only after ffprobe verifies the MP4), qa_passed, qa_failed, final_approved, blocked, failed, retry.

## Research handoff — candidates.json
`{"date","candidates":[{"rank","product_name","listing_url","category","price":{value,source_url,verified},"features":[{value,source_url,verified}],"benefits":[...],"target_audience","demand_evidence":[{summary,source_url}],"differentiation","angles":[...],"image_urls":[...],"risks":[...],"unverified":[...]}],"blockers":[]}`

## product_brief.json
Same fields for one product + `verified_claims_allowed:[...]` (the only claims creative may use) and local `reference_images:[paths]`.

## Creative handoff
- script.json `{"language","lines":[{"start","end","text","words"}],"est_duration_s"}`
- storyboard.json `{"scenes":[{"id","start","end","visual","camera","on_screen_text","reference_image","generation_method","keyframe_prompt","video_prompt","transition","sfx"}]}`
- captions.json `{"hook":{text,start,end},"captions":[{text,start,end}],"cta":{text,start,end},"disclosure":{text}}`

## QA handoff — qa_report.json
`{"video_id","verdict":"PASS|FAIL","checked_at","checks":[{"name","mandatory":true,"result":"pass|fail","evidence":"frame/timestamp/ffprobe value"}],"failures":[{"check","issue","route_to","fix_instruction"}]}`

## Agent return summary
Every agent ends with a one-line JSON summary (`status`, `files`, `blockers`). The Manager verifies files on disk before trusting it.
