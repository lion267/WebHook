#!/usr/bin/env bash
# Honest capability check. Prints JSON; never assumes a tool works.
have() { command -v "$1" >/dev/null 2>&1 && echo true || echo false; }
pymod() { python3 -I -c "import $1" >/dev/null 2>&1 && echo true || echo false; }
ver() { "$@" 2>&1 | head -1 | tr -d '"'; }

FFMPEG=$(have ffmpeg); FFPROBE=$(have ffprobe); DOTNET=$(have dotnet); NODE=$(have node)
TTS_CLI=false; for t in espeak espeak-ng piper say; do [ "$(have $t)" = true ] && TTS_CLI=true; done
TTS_PY=false; for m in edge_tts TTS pyttsx3 piper; do [ "$(pymod $m)" = true ] && TTS_PY=true; done
COMFY=false; { curl -s -m 2 http://127.0.0.1:8188/system_stats >/dev/null 2>&1; } && COMFY=true
PIL=$(pymod PIL); SQLITE_PY=$(pymod sqlite3)

blockers=()
[ "$FFMPEG" = false ] && blockers+=("ffmpeg_missing")
[ "$DOTNET" = false ] && blockers+=("dotnet_sdk_missing: ASP.NET Core dashboard cannot be built/run here")
[ "$TTS_CLI" = false ] && [ "$TTS_PY" = false ] && blockers+=("no_tts_engine: voiceover cannot be generated")
[ "$COMFY" = false ] && blockers+=("comfyui_not_reachable_on_127.0.0.1:8188")
blockers+=("image_video_generation: no generation tool verified by this script; check MCP servers/skills manually")
blockers+=("music: no licensed music source verified")

printf '{\n  "ffmpeg": %s, "ffprobe": %s, "node": %s, "dotnet": %s,\n  "python_pillow": %s, "python_sqlite3": %s,\n  "tts": %s, "comfyui": %s,\n  "blockers": [' \
  "$FFMPEG" "$FFPROBE" "$NODE" "$DOTNET" "$PIL" "$SQLITE_PY" "$([ $TTS_CLI = true ] || [ $TTS_PY = true ] && echo true || echo false)" "$COMFY"
first=1; for b in "${blockers[@]}"; do [ $first = 0 ] && printf ','; printf '\n    "%s"' "$b"; first=0; done
printf '\n  ]\n}\n'
