import json, os, shutil, subprocess, sys, tempfile, threading, time, unittest, urllib.request
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="studio_test_"))
os.environ["STUDIO_ROOT"] = str(TMP)
os.environ["STUDIO_DB"] = str(TMP / "test.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import studio  # noqa: E402

VID = "20261116-01-test"


def make_video(path, w=1080, h=1920, audio=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=blue:s={w}x{h}:d=1:r=30"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)


class T(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP / "studio", ignore_errors=True)
        Path(os.environ["STUDIO_DB"]).unlink(missing_ok=True)
        self.c = studio.connect()
        studio.create_job(self.c, VID, "Test product", "https://example.invalid/p")

    def test_create_is_idempotent(self):
        self.assertFalse(studio.create_job(self.c, VID, "Test product"))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 3)

    def test_claim_respects_dependencies_and_duplicates(self):
        self.assertEqual(studio.claim_task(self.c, f"{VID}:produce")[0], "blocked_by_deps")
        self.assertEqual(studio.claim_task(self.c, f"{VID}:concept")[0], "claimed")
        self.assertEqual(studio.claim_task(self.c, f"{VID}:concept")[0], "already_running")
        studio.finish_task(self.c, f"{VID}:concept", "completed")
        self.assertEqual(studio.claim_task(self.c, f"{VID}:concept")[0], "done")
        self.assertEqual(studio.claim_task(self.c, f"{VID}:produce")[0], "claimed")

    def test_recover_stale_then_fail_after_max_attempts(self):
        k = f"{VID}:concept"
        for i in range(studio.MAX_ATTEMPTS):
            self.assertEqual(studio.claim_task(self.c, k)[0], "claimed")
            self.c.execute("UPDATE tasks SET last_activity='2000-01-01T00:00:00+08:00' WHERE idempotency_key=?", (k,))
            r = studio.recover(self.c, 30)
            self.assertEqual(r[0]["now"], "failed" if i == studio.MAX_ATTEMPTS - 1 else "pending")

    def test_milestones_are_verified_against_files(self):
        with self.assertRaises(SystemExit):
            studio.milestone(self.c, VID, "video_verified")  # no file
        with self.assertRaises(SystemExit):
            studio.milestone(self.c, VID, "qa_pass")
        make_video(studio.job_dir(VID) / "production" / "final" / f"{VID}.mp4")
        studio.milestone(self.c, VID, "video_verified")
        with self.assertRaises(SystemExit):  # report still missing
            studio.milestone(self.c, VID, "qa_pass")
        (studio.job_dir(VID) / "qa" / "qa_report.json").write_text(json.dumps({"verdict": "FAIL"}))
        with self.assertRaises(SystemExit):
            studio.milestone(self.c, VID, "qa_pass")
        (studio.job_dir(VID) / "qa" / "qa_report.json").write_text(json.dumps({"verdict": "PASS"}))
        studio.milestone(self.c, VID, "qa_pass")

    def test_complete_requires_everything(self):
        with self.assertRaises(SystemExit):
            studio.complete_job(self.c, VID)
        make_video(studio.job_dir(VID) / "production" / "final" / f"{VID}.mp4")
        (studio.job_dir(VID) / "qa" / "qa_report.json").write_text(json.dumps({"verdict": "PASS"}))
        for m in ["research", "product_approved", "creative_ready", "skill_invoked", "video_verified", "qa_pass"]:
            studio.milestone(self.c, VID, m)
        with self.assertRaises(SystemExit):  # final approval still missing
            studio.complete_job(self.c, VID)
        studio.milestone(self.c, VID, "final_approved")
        info = studio.complete_job(self.c, VID)
        self.assertEqual((info["width"], info["height"]), (1080, 1920))
        self.assertEqual(studio.job_view(self.c, VID)["progress_pct"], 100)
        with self.assertRaises(SystemExit):
            studio.set_job(self.c, VID, status="COMPLETED")

    def test_complete_rejects_wrong_size_or_silent(self):
        make_video(studio.job_dir(VID) / "production" / "final" / f"{VID}.mp4", 720, 1280, audio=False)
        (studio.job_dir(VID) / "qa" / "qa_report.json").write_text(json.dumps({"verdict": "PASS"}))
        for m in studio.MILESTONES:
            self.c.execute("INSERT OR IGNORE INTO milestones VALUES(?,?,?,NULL)", (VID, m, studio.now()))
        with self.assertRaises(SystemExit):
            studio.complete_job(self.c, VID)

    def test_hook_logs_skill_and_tools(self):
        e1 = studio.hook(self.c, {"hook_event_name": "PreToolUse", "tool_name": "Skill",
                                  "tool_input": {"skill": "anthropic-skills:product-to-video-ad"}, "agent_type": "video-producer",
                                  "cwd": str(studio.job_dir(VID) / "production")})
        self.assertEqual(self.c.execute('SELECT video_id FROM events WHERE id=?', (e1,)).fetchone()[0], VID)  # job id inferred from cwd
        e2 = studio.hook(self.c, {"hook_event_name": "PostToolUse", "tool_name": "Write",
                                  "tool_input": {"file_path": f"studio/jobs/{VID}/creative/script.json"}})
        evs = [r["event"] for r in self.c.execute("SELECT event FROM events WHERE source='hook' ORDER BY id")]
        self.assertEqual(evs, ["skill_invoked", "tool_end"])
        self.assertEqual(self.c.execute("SELECT video_id FROM events WHERE id=?", (e2,)).fetchone()[0], VID)
        self.assertIsNone(studio.hook(self.c, {"hook_event_name": "Notification"}))

    def test_overview_shows_hook_only_agents(self):
        # real SubagentStart payload shape observed in a Claude Code session
        studio.hook(self.c, {"hook_event_name": "SubagentStart", "agent_id": "a1", "agent_type": "product-researcher", "cwd": "/x"})
        w = {x["agent"]: x for x in studio.overview(self.c)["workers"]}
        self.assertEqual(w["product-researcher"]["status"], "running")
        studio.hook(self.c, {"hook_event_name": "SubagentStop", "agent_id": "a1", "agent_type": "product-researcher", "cwd": "/x"})
        w = {x["agent"]: x for x in studio.overview(self.c)["workers"]}
        self.assertEqual(w["product-researcher"]["status"], "completed")

    def test_hook_cli_never_fails(self):
        r = subprocess.run([sys.executable, str(Path(studio.__file__)), "hook"], input="not json", capture_output=True, text=True,
                           env=os.environ)
        self.assertEqual(r.returncode, 0)

    def test_api_and_sse(self):
        port = 18787
        threading.Thread(target=studio.serve, args=(port,), daemon=True).start()
        time.sleep(0.5)
        studio.add_event(self.c, VID, "creative-director", "script_generated")
        ov = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview"))
        self.assertEqual(ov["in_production"], 1)
        job = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/jobs/{VID}"))
        self.assertEqual(job["progress_pct"], 0)
        req = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stream?after=0", timeout=5)
        line = b""
        while b"data:" not in line:
            line = req.readline()
        self.assertIn(b"job_created", line)
        req.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
