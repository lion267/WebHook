---
name: video-qa
description: QA checklist and ffmpeg/ffprobe commands for judging a finished 9:16 Shopee affiliate video - technical specs, audio, product fidelity, captions, claims, artifacts - with mandatory checks and PASS/FAIL rules.
---

# Video QA checklist

## Commands
```bash
V=studio/jobs/$ID/production/final/$ID.mp4
ffprobe -v error -show_entries stream=codec_type,codec_name,width,height,r_frame_rate,duration,sample_rate -show_entries format=duration,size -of json "$V"
ffmpeg -v error -i "$V" -af volumedetect -vn -f null - 2>&1 | grep -E "mean_volume|max_volume"
mkdir -p studio/jobs/$ID/qa/frames
ffmpeg -v error -i "$V" -vf "fps=2,scale=540:-1" -frames:v 40 studio/jobs/$ID/qa/frames/f_%02d.jpg   # hook + overview
ffmpeg -v error -ss 19 -i "$V" -frames:v 1 studio/jobs/$ID/qa/frames/end.jpg
```
View frames with Read before reporting on them.

## Mandatory (any fail = FAIL)
1. File exists, plays, size > 0.
2. 1080x1920, h264 video + aac audio, 28-32 fps, duration 18-22 s.
3. Audible voiceover (mean volume > -35 dB, not clipped: max < 0 dB) unless user requested text-only.
4. Hook in first 3 s: visual action + text + VO + sound.
5. Product fidelity vs reference (shape, proportions, color, branding, material) in hook, mid and end frames.
6. Text spelled correctly, inside safe margins, not covering product; captions match VO.
7. CTA ("Shop Now on Shopee") visible ≥ 2 s; disclosure present when required.
8. Every claim maps to a verified field. No fake price/discount/rating/urgency/scarcity.
9. No morphing/flicker/bad anatomy/floating objects; no static composition > 3 s; ≥ 6 visual beats; no big black areas.
10. Music below VO; no third-party watermark.

## Reporting
Record each check with evidence. FAIL entries get `route_to` and a concrete fix. Not able to view or measure something = that check FAILS as "unverifiable", never assumed pass.
