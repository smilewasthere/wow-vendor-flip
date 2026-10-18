import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ScriptEntrypointTest(unittest.TestCase):
    r"""start.cmd runs `python app\main.py` — the script must boot from that path."""

    def test_main_py_boots_and_serves_health(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump({"host": "127.0.0.1", "port": 0,
                       "tsm_boot_refresh": False,
                       "auctions_boot_refresh": False}, fh)
            cfg_path = fh.name
        proc = subprocess.Popen(
            [sys.executable, os.path.join("app", "main.py"), "--no-browser",
             "--config", cfg_path],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            line = proc.stdout.readline().strip()
            self.assertTrue(
                line.startswith("wow-vendor-flip listening on"),
                "unexpected banner: %r (stderr: %s)"
                % (line, proc.stderr.read() if proc.poll() is not None else ""),
            )
            url = line.split("listening on ", 1)[1].rstrip("/")
            with urllib.request.urlopen(url + "/api/health", timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(payload["status"], "ok")
        finally:
            proc.kill()
            proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()
            os.unlink(cfg_path)


    def test_boot_refresh_is_config_gated(self):
        with open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("tsm_boot_refresh", src,
                      "boot TSM refresh must honor the config key")
        self.assertIn("refresh_async", src,
                      "main.py wires the T1 refresh worker")
        self.assertIn("auctions_boot_refresh", src,
                      "boot auction-wall refresh must honor its config key")
        self.assertIn("auctions.refresh_async", src,
                      "main.py wires the M16 refresh worker")


if __name__ == "__main__":
    unittest.main()
