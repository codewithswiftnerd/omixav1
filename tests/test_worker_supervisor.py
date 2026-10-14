"""Stage 4: OMIXA_WORKER_PROCESSES supervisor: bounded, restarts crashed workers, stops gracefully."""
import os, signal, subprocess, sys, time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(tmp_path, procs):
    return dict(os.environ, OMIXA_SECRET_KEY="x" * 48, OMIXA_PROCESSING_MODE="queue", OMIXA_TEMP_DIR=str(tmp_path / "t"),
                OMIXA_DB_PATH=str(tmp_path / "o.db"), OMIXA_ALLOW_SINGLE_NODE="1", OMIXA_LOG_FORMAT="plain",
                OMIXA_WORKER_PROCESSES=str(procs), PYTHONPATH=ROOT)


def _children(parent_pid):
    out = subprocess.run(["ps", "-o", "pid=", "--ppid", str(parent_pid)], capture_output=True, text=True).stdout.split()
    return [int(p) for p in out]


@pytest.mark.parametrize("raw,expected", [("1", 1), ("4", 4), ("0", 1), ("-3", 1), ("999", 16), ("", 1)])
def test_worker_process_count_is_bounded(raw, expected):
    code = ("import os,sys; os.environ['OMIXA_SECRET_KEY']='x'*48; os.environ['OMIXA_WORKER_PROCESSES']=%r; "
            "sys.path.insert(0,%r); from config import Config; print(Config.WORKER_PROCESSES)" % (raw, ROOT))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT).stdout.strip().splitlines()[-1]
    assert int(out) == expected


def test_supervisor_runs_n_workers_restarts_a_crashed_one_and_stops_cleanly(tmp_path):
    os.makedirs(tmp_path / "t", exist_ok=True)
    sup = subprocess.Popen([sys.executable, "worker.py"], cwd=ROOT, env=_env(tmp_path, 3),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            kids = _children(sup.pid)
            if len(kids) == 3: break
            time.sleep(0.25)
        assert len(kids) == 3
        os.kill(kids[0], signal.SIGKILL)                      # simulate a worker crash
        for _ in range(80):
            now = _children(sup.pid)
            if len(now) == 3 and kids[0] not in now: break
            time.sleep(0.25)
        assert len(now) == 3 and kids[0] not in now           # restarted, still bounded at 3
        sup.send_signal(signal.SIGTERM)
        assert sup.wait(timeout=30) == 0                      # graceful: workers drained, supervisor exits 0
        assert _children(sup.pid) == []
    finally:
        if sup.poll() is None:
            sup.kill()
