"""Helper for tests/test_concurrent_workers.py (not collected by pytest: no test_ prefix).

Starts REAL `worker.py` subprocesses (queue mode, shared SQLite + local storage, no Redis), has two
independent devices (separate sessions) upload different files and start cleaning at the same moment,
and prints one JSON line: when each job was first seen running/processed, whether each device got back
exactly its own rows, and whether one device can read the other's job. Polls at ~2 Hz so the app's
rate limiter (and a 1-CPU box) are not part of what is measured.
Usage: python tests/concurrency_driver.py <worker_processes> <rows>
"""
import io, json, os, signal, subprocess, sys, tempfile, threading, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
N, ROWS = int(sys.argv[1]), int(sys.argv[2])
T = tempfile.mkdtemp(prefix="omixa-conc-")
os.environ.update(
    OMIXA_SECRET_KEY="k" * 48, FLASK_ENV="development", OMIXA_PROCESSING_MODE="queue",
    OMIXA_DB_PATH=T + "/o.db", OMIXA_TEMP_DIR=T + "/tmp", OMIXA_JOB_MEMORY_LIMIT_MB="0",
    OMIXA_WORKER_PROCESSES=str(N), RATE_LIMIT_PER_MINUTE="0", OMIXA_LOG_FORMAT="plain",
)
sys.path.insert(0, REPO); os.chdir(REPO)
import pandas as pd  # noqa: E402
import app as app_module  # noqa: E402

def frame(prefix):
    return pd.DataFrame({
        "Transaction ID": [f"{prefix}{i:06d}" for i in range(ROWS)],
        "Item": [("Tea", "Cake", "Salad", "Juice")[i % 4] for i in range(ROWS)],
        "Quantity": [str(1 + i % 5) for i in range(ROWS)],
        "Total Spent": [("3.0", "ERROR", "", "12.5")[i % 4] for i in range(ROWS)],
        "Transaction Date": [("2023-01-05", "UNKNOWN", "", "2023-02-06")[i % 4] for i in range(ROWS)],
    })

app = app_module.create_app()
dev = {"A": app.test_client(), "B": app.test_client()}
files = {"A": frame("A_"), "B": frame("B_")}
job = {}
for k, df in files.items():
    r = dev[k].post("/api/upload/", data={"file": (io.BytesIO(df.to_csv(index=False).encode()), k + ".csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    job[k] = r.get_json()["job_id"]

log = open(T + "/worker.log", "w")
w = subprocess.Popen([sys.executable, "worker.py"], cwd=REPO, env=dict(os.environ), stdout=log, stderr=subprocess.STDOUT)
time.sleep(4)  # worker boot time must not count as queueing
t0 = time.time(); seen = {k: {} for k in job}; codes = {}
def submit(k):
    codes[k] = dev[k].post(f"/api/process/{job[k]}", json={"rules": ["formatting", "missing_token_normalization", "duplicates"]}).status_code
ths = [threading.Thread(target=submit, args=(k,)) for k in job]; [t.start() for t in ths]; [t.join() for t in ths]
while time.time() - t0 < 240 and not all({"processed", "failed"} & set(seen[k]) for k in job):
    for k in job:
        s = dev[k].get(f"/api/jobs/{job[k]}").get_json().get("status")
        seen[k].setdefault(s, round(time.time() - t0, 2))
    time.sleep(0.5)
res = {"workers": N, "submit_codes": codes, "seen": seen, "own_rows_only": {}}
for k in job:
    r = dev[k].get(f"/api/download/{job[k]}")
    out = pd.read_csv(io.BytesIO(r.data), dtype=str, keep_default_na=False) if r.status_code == 200 else None
    res["own_rows_only"][k] = bool(out is not None and len(out) == ROWS
                                   and set(out.iloc[:, 0]) == set(files[k]["Transaction ID"]))
res["cross_access"] = [dev["B"].get(f"/api/jobs/{job['A']}").status_code, dev["B"].get(f"/api/download/{job['A']}").status_code]
w.send_signal(signal.SIGTERM)
try: w.wait(timeout=30)
except Exception: w.kill()
print("RESULT " + json.dumps(res))
