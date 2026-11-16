#!/usr/bin/env python3
"""Affiliate studio state: SQLite job tracker, Claude Code hook logger, read-only JSON/SSE API.

Stdlib only. DB: $STUDIO_DB or <project>/studio/studio.db. All output is JSON.
Truth rules: milestones that claim a video exists or QA passed are checked against files on disk.
"""
import argparse, hashlib, json, os, re, sqlite3, subprocess, sys, time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(os.environ.get("STUDIO_ROOT", Path(__file__).resolve().parents[2]))
DB_PATH = Path(os.environ.get("STUDIO_DB", ROOT / "studio" / "studio.db"))
TZ = timezone(timedelta(hours=8))
MILESTONES = ["research", "product_approved", "creative_ready", "skill_invoked",
              "video_verified", "qa_pass", "final_approved"]
STAGES = ["DISCOVERY", "RESEARCH", "PRODUCT_APPROVAL", "CONCEPT", "SCRIPT_STORYBOARD", "VIDEO_PRODUCTION",
          "QA", "FINAL_APPROVAL", "COMPLETED", "BLOCKED", "FAILED", "REJECTED"]
JOB_STATUS = ["ACTIVE", "AWAITING_APPROVAL", "BLOCKED", "FAILED", "NEEDS_HUMAN", "REJECTED", "COMPLETED"]
TASK_STATUS = ["pending", "running", "completed", "failed", "blocked"]
MAX_ATTEMPTS = 3
JOB_TASKS = [("concept", "creative-director", []),
             ("produce", "video-producer", ["concept"]),
             ("qa", "qa-specialist", ["produce"])]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  video_id TEXT PRIMARY KEY, product_name TEXT NOT NULL, product_url TEXT,
  stage TEXT NOT NULL DEFAULT 'CONCEPT', status TEXT NOT NULL DEFAULT 'ACTIVE',
  assigned_worker TEXT, revision_loops INTEGER NOT NULL DEFAULT 0, final_path TEXT,
  blockers TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS milestones(
  video_id TEXT NOT NULL REFERENCES jobs(video_id), name TEXT NOT NULL, done_at TEXT NOT NULL,
  evidence TEXT, PRIMARY KEY(video_id, name));
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL REFERENCES jobs(video_id),
  name TEXT NOT NULL, agent TEXT NOT NULL, depends_on TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT NOT NULL UNIQUE, started_at TEXT, last_activity TEXT, finished_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS executions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id),
  attempt INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, video_id TEXT, agent TEXT,
  event TEXT NOT NULL, detail TEXT, artifact TEXT, source TEXT NOT NULL DEFAULT 'agent');
CREATE INDEX IF NOT EXISTS ev_video ON events(video_id, id);
CREATE TABLE IF NOT EXISTS assets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL REFERENCES jobs(video_id),
  kind TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT, created_at TEXT NOT NULL, UNIQUE(video_id, path));
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL REFERENCES jobs(video_id),
  gate TEXT NOT NULL, decision TEXT NOT NULL, notes TEXT, ts TEXT NOT NULL);
"""


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def connect(path=None):
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    return c


def job_dir(video_id):
    return ROOT / "studio" / "jobs" / video_id


def add_event(c, video_id, agent, event, detail=None, artifact=None, source="agent"):
    ts = now()
    cur = c.execute("INSERT INTO events(ts,video_id,agent,event,detail,artifact,source) VALUES(?,?,?,?,?,?,?)",
                    (ts, video_id, agent, event, detail, artifact, source))
    if video_id and job_dir(video_id).is_dir():  # human-readable mirror
        with open(job_dir(video_id) / "events.jsonl", "a") as f:
            f.write(json.dumps({"ts": ts, "video_id": video_id, "agent": agent, "event": event,
                                "detail": detail, "artifact": artifact, "source": source}) + "\n")
    return cur.lastrowid


def touch(c, video_id):
    c.execute("UPDATE jobs SET updated_at=? WHERE video_id=?", (now(), video_id))


def require_job(c, video_id):
    if not c.execute("SELECT 1 FROM jobs WHERE video_id=?", (video_id,)).fetchone():
        raise SystemExit(f"unknown job: {video_id}")


# ---------- jobs ----------
def create_job(c, video_id, product_name, url=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]{2,80}", video_id):
        raise SystemExit("video_id must be 3-81 chars: letters, digits, _ or -")
    t = now()
    cur = c.execute("INSERT OR IGNORE INTO jobs(video_id,product_name,product_url,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (video_id, product_name, url, t, t))
    created = cur.rowcount == 1
    for sub in ["research/images", "creative", "production/approved", "production/final", "qa/frames"]:
        (job_dir(video_id) / sub).mkdir(parents=True, exist_ok=True)
    for name, agent, deps in JOB_TASKS:
        c.execute("INSERT OR IGNORE INTO tasks(video_id,name,agent,depends_on,idempotency_key) VALUES(?,?,?,?,?)",
                  (video_id, name, agent, json.dumps(deps), f"{video_id}:{name}"))
    if created:
        add_event(c, video_id, "production-manager", "job_created", product_name, source="manager")
    return created


def set_job(c, video_id, stage=None, status=None, worker=None, blocker=None, clear_blockers=False):
    require_job(c, video_id)
    if stage and stage not in STAGES:
        raise SystemExit(f"stage must be one of {STAGES}")
    if status and status not in JOB_STATUS:
        raise SystemExit(f"status must be one of {JOB_STATUS}")
    if status == "COMPLETED":
        raise SystemExit("use job-complete (it verifies the MP4, QA and final approval)")
    if stage == "COMPLETED":
        raise SystemExit("use job-complete")
    row = c.execute("SELECT blockers FROM jobs WHERE video_id=?", (video_id,)).fetchone()
    bl = [] if clear_blockers else json.loads(row["blockers"])
    if blocker:
        bl.append(blocker)
    c.execute("UPDATE jobs SET stage=COALESCE(?,stage), status=COALESCE(?,status), assigned_worker=COALESCE(?,assigned_worker),"
              " blockers=?, updated_at=? WHERE video_id=?", (stage, status, worker, json.dumps(bl), now(), video_id))
    add_event(c, video_id, "production-manager", "job_updated",
              json.dumps({"stage": stage, "status": status, "worker": worker, "blocker": blocker}), source="manager")


def ffprobe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate",
                          "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise SystemExit(f"ffprobe failed: {out.stderr.strip()[:200]}")
    d = json.loads(out.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    if not v:
        raise SystemExit("no video stream")
    num, den = v["r_frame_rate"].split("/")
    return {"width": v["width"], "height": v["height"], "vcodec": v["codec_name"], "fps": round(int(num) / max(int(den), 1), 2),
            "acodec": a["codec_name"] if a else None, "duration": round(float(d["format"]["duration"]), 2)}


def milestone(c, video_id, name, evidence=None):
    require_job(c, video_id)
    if name not in MILESTONES:
        raise SystemExit(f"milestone must be one of {MILESTONES}")
    if name == "video_verified":
        p = job_dir(video_id) / "production" / "final" / f"{video_id}.mp4"
        if not p.is_file() or p.stat().st_size == 0:
            raise SystemExit(f"refused: {p} does not exist")
        info = ffprobe(p)
        evidence = json.dumps(info)
        c.execute("UPDATE jobs SET final_path=? WHERE video_id=?", (str(p), video_id))
    if name == "qa_pass":
        r = job_dir(video_id) / "qa" / "qa_report.json"
        if not r.is_file() or json.loads(r.read_text()).get("verdict") != "PASS":
            raise SystemExit("refused: qa/qa_report.json missing or verdict is not PASS")
        if not c.execute("SELECT 1 FROM milestones WHERE video_id=? AND name='video_verified'", (video_id,)).fetchone():
            raise SystemExit("refused: video_verified must come before qa_pass")
    c.execute("INSERT OR IGNORE INTO milestones(video_id,name,done_at,evidence) VALUES(?,?,?,?)", (video_id, name, now(), evidence))
    touch(c, video_id)
    add_event(c, video_id, "production-manager", f"milestone_{name}", evidence, source="manager")


def complete_job(c, video_id):
    require_job(c, video_id)
    have = {r["name"] for r in c.execute("SELECT name FROM milestones WHERE video_id=?", (video_id,))}
    missing = [m for m in MILESTONES if m not in have]
    if missing:
        raise SystemExit(f"refused: missing milestones {missing}")
    p = job_dir(video_id) / "production" / "final" / f"{video_id}.mp4"
    info = ffprobe(p)  # re-verify at completion
    if (info["width"], info["height"]) != (1080, 1920) or not info["acodec"]:
        raise SystemExit(f"refused: final video is not 1080x1920 with audio: {info}")
    c.execute("UPDATE jobs SET stage='COMPLETED', status='COMPLETED', final_path=?, updated_at=? WHERE video_id=?", (str(p), now(), video_id))
    add_event(c, video_id, "production-manager", "job_completed", json.dumps(info), str(p), "manager")
    return info


# ---------- tasks ----------
def claim_task(c, key):
    """Atomic pending->running. Returns ('claimed'|'already_running'|'done'|'blocked_by_deps'|'max_attempts', row)."""
    c.execute("BEGIN IMMEDIATE")
    try:
        t = c.execute("SELECT * FROM tasks WHERE idempotency_key=?", (key,)).fetchone()
        if not t:
            c.execute("ROLLBACK"); raise SystemExit(f"unknown task {key}")
        if t["status"] == "running":
            c.execute("ROLLBACK"); return "already_running", t
        if t["status"] == "completed":
            c.execute("ROLLBACK"); return "done", t
        for dep in json.loads(t["depends_on"]):
            d = c.execute("SELECT status FROM tasks WHERE idempotency_key=?", (f"{t['video_id']}:{dep}",)).fetchone()
            if not d or d["status"] != "completed":
                c.execute("ROLLBACK"); return "blocked_by_deps", t
        if t["attempts"] >= MAX_ATTEMPTS:
            c.execute("ROLLBACK"); return "max_attempts", t
        ts = now()
        c.execute("UPDATE tasks SET status='running',attempts=attempts+1,started_at=?,last_activity=?,finished_at=NULL,error=NULL WHERE id=?",
                  (ts, ts, t["id"]))
        c.execute("INSERT INTO executions(task_id,attempt,started_at) VALUES(?,?,?)", (t["id"], t["attempts"] + 1, ts))
        c.execute("UPDATE jobs SET assigned_worker=?, updated_at=? WHERE video_id=?", (t["agent"], ts, t["video_id"]))
        c.execute("COMMIT")
    except sqlite3.Error:
        c.execute("ROLLBACK"); raise
    add_event(c, t["video_id"], t["agent"], "task_started", t["name"], source="manager")
    return "claimed", c.execute("SELECT * FROM tasks WHERE id=?", (t["id"],)).fetchone()


def finish_task(c, key, status, error=None):
    if status not in ("completed", "failed", "blocked"):
        raise SystemExit("status must be completed|failed|blocked")
    t = c.execute("SELECT * FROM tasks WHERE idempotency_key=?", (key,)).fetchone()
    if not t:
        raise SystemExit(f"unknown task {key}")
    ts = now()
    c.execute("UPDATE tasks SET status=?,finished_at=?,error=? WHERE id=?", (status, ts, error, t["id"]))
    c.execute("UPDATE executions SET finished_at=?,outcome=?,error=? WHERE task_id=? AND finished_at IS NULL", (ts, status, error, t["id"]))
    add_event(c, t["video_id"], t["agent"], f"task_{status}", f"{t['name']}: {error}" if error else t["name"], source="manager")
    touch(c, t["video_id"])


def recover(c, stale_minutes=30):
    """After a restart/crash: tasks stuck 'running' with no activity go back to pending, or failed after MAX_ATTEMPTS."""
    cutoff = (datetime.now(TZ) - timedelta(minutes=stale_minutes)).isoformat(timespec="seconds")
    out = []
    for t in c.execute("SELECT * FROM tasks WHERE status='running' AND COALESCE(last_activity,started_at) < ?", (cutoff,)).fetchall():
        new = "failed" if t["attempts"] >= MAX_ATTEMPTS else "pending"
        c.execute("UPDATE tasks SET status=?, error=? WHERE id=?", (new, "stale: no activity, recovered", t["id"]))
        c.execute("UPDATE executions SET finished_at=?,outcome='stale' WHERE task_id=? AND finished_at IS NULL", (now(), t["id"]))
        add_event(c, t["video_id"], t["agent"], "retry" if new == "pending" else "failed", f"{t['name']} recovered as {new}", source="manager")
        out.append({"task": t["idempotency_key"], "now": new})
    return out


# ---------- views ----------
def job_view(c, video_id, events=50):
    j = c.execute("SELECT * FROM jobs WHERE video_id=?", (video_id,)).fetchone()
    if not j:
        return None
    ms = {r["name"]: {"done_at": r["done_at"], "evidence": r["evidence"]} for r in c.execute("SELECT * FROM milestones WHERE video_id=?", (video_id,))}
    d = dict(j)
    d["blockers"] = json.loads(d["blockers"])
    d["milestones"] = ms
    d["progress_pct"] = round(100 * len(ms) / len(MILESTONES))
    d["tasks"] = [dict(r) for r in c.execute("SELECT * FROM tasks WHERE video_id=? ORDER BY id", (video_id,))]
    d["assets"] = [dict(r) for r in c.execute("SELECT * FROM assets WHERE video_id=? ORDER BY id", (video_id,))]
    d["approvals"] = [dict(r) for r in c.execute("SELECT * FROM approvals WHERE video_id=? ORDER BY id", (video_id,))]
    d["events"] = [dict(r) for r in c.execute("SELECT * FROM events WHERE video_id=? ORDER BY id DESC LIMIT ?", (video_id, events))][::-1]
    return d


def overview(c, target=5):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    g = lambda q, *a: c.execute(q, a).fetchone()[0]
    workers = [dict(r) for r in c.execute(
        "SELECT agent, name AS task, video_id, status, started_at, last_activity, error FROM tasks t WHERE id IN "
        "(SELECT MAX(id) FROM tasks GROUP BY agent)")]
    # Agents observed only via Claude Code hooks (e.g. research before any job exists): status from lifecycle events.
    known = {w["agent"] for w in workers if w["status"] == "running"}
    for r in c.execute("SELECT agent, MAX(ts) AS last FROM events WHERE source='hook' AND agent!='main' GROUP BY agent").fetchall():
        if r["agent"] in known:
            continue
        lc = c.execute("SELECT event, ts, video_id FROM events WHERE source='hook' AND agent=? AND event IN ('agent_start','agent_stop')"
                       " ORDER BY id DESC LIMIT 1", (r["agent"],)).fetchone()
        st = "running" if lc and lc["event"] == "agent_start" else "completed"
        workers = [w for w in workers if w["agent"] != r["agent"]]
        workers.append({"agent": r["agent"], "task": "(hook)", "video_id": lc["video_id"] if lc else None, "status": st,
                        "started_at": lc["ts"] if lc else None, "last_activity": r["last"], "error": None})
    return {"date": today, "daily_target": target,
            "completed_today": g("SELECT COUNT(*) FROM jobs WHERE status='COMPLETED' AND substr(updated_at,1,10)=?", today),
            "in_production": g("SELECT COUNT(*) FROM jobs WHERE status='ACTIVE'"),
            "blocked_or_failed": g("SELECT COUNT(*) FROM jobs WHERE status IN ('BLOCKED','FAILED','NEEDS_HUMAN')"),
            "awaiting_approval": g("SELECT COUNT(*) FROM jobs WHERE status='AWAITING_APPROVAL'"),
            "workers": workers,
            "last_event_id": g("SELECT COALESCE(MAX(id),0) FROM events")}


# ---------- hook logger ----------
def hook(c, payload):
    """Claude Code hook -> events. Field names follow Claude Code's documented hook input; unknown fields are tolerated."""
    name = payload.get("hook_event_name", "")
    tool = payload.get("tool_name")
    tin = payload.get("tool_input") or {}
    agent = payload.get("agent_type") or payload.get("subagent_type") or payload.get("agent_name") or "main"
    blob = json.dumps(tin)[:2000]
    m = re.search(r"jobs/([A-Za-z0-9][A-Za-z0-9_\-]{2,80})/", blob) or re.search(r"jobs/([A-Za-z0-9][A-Za-z0-9_\-]{2,80})/", str(payload.get("cwd", "")))
    vid = os.environ.get("STUDIO_VIDEO_ID") or (m.group(1) if m else None)
    if vid and not c.execute("SELECT 1 FROM jobs WHERE video_id=?", (vid,)).fetchone():
        vid = None
    is_skill = tool == "Skill" and "product-to-video-ad" in json.dumps(tin)
    ev = None
    if name == "PreToolUse":
        ev = "skill_invoked" if is_skill else "tool_start"
    elif name == "PostToolUse":
        ev = "skill_finished" if is_skill else "tool_end"
    elif name == "SubagentStart":
        ev = "agent_start"
    elif name == "SubagentStop":
        ev = "agent_stop"
    if not ev:
        return None
    detail = f"{tool}: {blob[:200]}" if tool else (payload.get("agent_id") or "")
    eid = add_event(c, vid, agent, ev, detail, None, "hook")
    if vid:
        c.execute("UPDATE tasks SET last_activity=? WHERE video_id=? AND status='running'", (now(), vid))
        if ev == "skill_invoked":
            c.execute("INSERT OR IGNORE INTO milestones(video_id,name,done_at,evidence) VALUES(?,?,?,?)",
                      (vid, "skill_invoked", now(), "observed via PreToolUse hook"))
    return eid


# ---------- read-only API ----------
class API(BaseHTTPRequestHandler):
    server_version = "studio/1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")  # read-only API bound to 127.0.0.1
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        c = connect()
        try:
            if u.path in ("/", "/index.html"):
                b = (ROOT / "studio" / "dashboard" / "index.html").read_bytes()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
            if u.path == "/api/overview":
                return self._json(overview(c))
            if u.path == "/api/jobs":
                return self._json([job_view(c, r[0], 0) for r in c.execute("SELECT video_id FROM jobs ORDER BY created_at DESC")])
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9_\-]+)", u.path)
            if m:
                v = job_view(c, m.group(1))
                return self._json(v or {"error": "not found"}, 200 if v else 404)
            if u.path == "/api/events":
                after = int(q.get("after", ["0"])[0])
                return self._json([dict(r) for r in c.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 500", (after,))])
            if u.path == "/api/stream":
                return self._stream(c, int(q.get("after", [self.headers.get("Last-Event-ID") or "0"])[0]))
            return self._json({"error": "not found"}, 404)
        finally:
            c.close()

    def _stream(self, c, last):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        idle = 0
        try:
            while True:
                rows = c.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 200", (last,)).fetchall()
                for r in rows:
                    last = r["id"]
                    self.wfile.write(f"id: {last}\nevent: studio\ndata: {json.dumps(dict(r))}\n\n".encode())
                idle = 0 if rows else idle + 1
                if idle and idle % 15 == 0:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, sqlite3.Error):
            pass


def serve(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), API)
    print(json.dumps({"serving": f"http://127.0.0.1:{port}", "db": str(DB_PATH)}), flush=True)
    srv.serve_forever()


# ---------- CLI ----------
def out(x):
    print(json.dumps(x, indent=2, default=str))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("init")
    a = s.add_parser("job-create"); a.add_argument("video_id"); a.add_argument("product_name"); a.add_argument("--url")
    a = s.add_parser("job-set"); a.add_argument("video_id"); a.add_argument("--stage"); a.add_argument("--status"); a.add_argument("--worker"); a.add_argument("--blocker"); a.add_argument("--clear-blockers", action="store_true")
    a = s.add_parser("job-show"); a.add_argument("video_id")
    s.add_parser("job-list")
    a = s.add_parser("job-complete"); a.add_argument("video_id")
    a = s.add_parser("milestone"); a.add_argument("video_id"); a.add_argument("name"); a.add_argument("--evidence")
    a = s.add_parser("task-claim"); a.add_argument("key", help="<video_id>:<concept|produce|qa>")
    a = s.add_parser("task-finish"); a.add_argument("key"); a.add_argument("status"); a.add_argument("--error")
    a = s.add_parser("event"); a.add_argument("video_id"); a.add_argument("agent"); a.add_argument("event"); a.add_argument("--detail"); a.add_argument("--artifact")
    a = s.add_parser("asset-add"); a.add_argument("video_id"); a.add_argument("kind"); a.add_argument("path")
    a = s.add_parser("approve"); a.add_argument("video_id"); a.add_argument("gate", choices=["product", "final"]); a.add_argument("decision", choices=["approved", "rejected", "revise"]); a.add_argument("--notes")
    a = s.add_parser("recover"); a.add_argument("--stale-minutes", type=int, default=30)
    s.add_parser("overview")
    s.add_parser("hook")
    a = s.add_parser("serve"); a.add_argument("--port", type=int, default=8787)
    n = p.parse_args(argv)

    if n.cmd == "hook":  # must never break a Claude Code session
        try:
            hook(connect(), json.loads(sys.stdin.read() or "{}"))
        except Exception as e:  # noqa
            print(f"studio hook error: {e}", file=sys.stderr)
        return 0
    if n.cmd == "serve":
        return serve(n.port)
    c = connect()
    if n.cmd == "init":
        out({"db": str(DB_PATH), "ok": True})
    elif n.cmd == "job-create":
        out({"created": create_job(c, n.video_id, n.product_name, n.url)})
    elif n.cmd == "job-set":
        set_job(c, n.video_id, n.stage, n.status, n.worker, n.blocker, n.clear_blockers); out({"ok": True})
    elif n.cmd == "job-show":
        v = job_view(c, n.video_id); out(v) if v else sys.exit(f"unknown job {n.video_id}")
    elif n.cmd == "job-list":
        out([{k: j[k] for k in ("video_id", "product_name", "stage", "status", "progress_pct")} for j in
             (job_view(c, r[0], 0) for r in c.execute("SELECT video_id FROM jobs ORDER BY created_at"))])
    elif n.cmd == "job-complete":
        out({"completed": True, "ffprobe": complete_job(c, n.video_id)})
    elif n.cmd == "milestone":
        milestone(c, n.video_id, n.name, n.evidence); out({"ok": True})
    elif n.cmd == "task-claim":
        r, t = claim_task(c, n.key); out({"result": r, "task": t["idempotency_key"], "attempts": t["attempts"]})
        return 0 if r == "claimed" else 3
    elif n.cmd == "task-finish":
        finish_task(c, n.key, n.status, n.error); out({"ok": True})
    elif n.cmd == "event":
        require_job(c, n.video_id); out({"id": add_event(c, n.video_id, n.agent, n.event, n.detail, n.artifact)})
    elif n.cmd == "asset-add":
        require_job(c, n.video_id)
        f = Path(n.path)
        if not f.is_file():
            raise SystemExit(f"refused: {f} does not exist")
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        c.execute("INSERT OR IGNORE INTO assets(video_id,kind,path,sha256,created_at) VALUES(?,?,?,?,?)", (n.video_id, n.kind, str(f), sha, now()))
        add_event(c, n.video_id, "production-manager", "asset_registered", n.kind, str(f), "manager"); out({"sha256": sha})
    elif n.cmd == "approve":
        require_job(c, n.video_id)
        c.execute("INSERT INTO approvals(video_id,gate,decision,notes,ts) VALUES(?,?,?,?,?)", (n.video_id, n.gate, n.decision, n.notes, now()))
        add_event(c, n.video_id, "human", f"{n.gate}_{n.decision}", n.notes, source="manager")
        if n.decision == "approved":
            milestone(c, n.video_id, "product_approved" if n.gate == "product" else "final_approved")
        out({"ok": True})
    elif n.cmd == "recover":
        out({"recovered": recover(c, n.stale_minutes)})
    elif n.cmd == "overview":
        out(overview(c))
    return 0


if __name__ == "__main__":
    sys.exit(main())
