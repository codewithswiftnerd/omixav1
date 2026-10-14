"""Live concurrency check: real worker.py processes, a shared database, two independent 'devices'.

    python scripts/check_concurrency.py --workers 1     # reproduces the one-job-at-a-time queue
    python scripts/check_concurrency.py --workers 2     # two jobs should overlap

Uses the supported queue mode with SQLite + local storage in a throw-away directory (no Redis: workers poll
the database). It measures when each job actually RAN, so on a 1-CPU machine the proof is overlapping
execution windows, not faster wall-clock time. Re-run it against your real deployment topology."""
import argparse, csv, io, os, random, sqlite3, subprocess, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_csv(prefix, n, seed):
    rnd = random.Random(seed); buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["Order ID", "Customer", "Email", "Phone", "Amount", "Order Date", "City"])
    for i in range(n):
        w.writerow([f"{prefix}_{i:06d}", f"  Customer {rnd.randint(1, 900)} ", f"U{rnd.randint(1,999)}@Example.com ",
                    f"0803{rnd.randint(1000000, 9999999)}", f"{rnd.randint(10, 9999)}.{rnd.randint(0, 99):02d}",
                    f"2024-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}", rnd.choice(["lagos", "ABUJA", "Port Harcourt", ""])])
    return buf.getvalue().encode()


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--workers", type=int, default=2, help="separate worker containers (replicas)")
    ap.add_argument("--processes", type=int, default=1, help="OMIXA_WORKER_PROCESSES inside each container")
    ap.add_argument("--rows", type=int, default=6000); ap.add_argument("--timeout", type=float, default=240)
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="omixa-conc-")
    env = dict(os.environ, OMIXA_SECRET_KEY="x" * 48, OMIXA_PROCESSING_MODE="queue", OMIXA_TEMP_DIR=f"{tmp}/temp",
               OMIXA_DB_PATH=f"{tmp}/omixa.db", OMIXA_JOB_MEMORY_LIMIT_MB="0", OMIXA_LOG_FORMAT="plain",
               OMIXA_STORAGE_BACKEND="local", OMIXA_ALLOW_SINGLE_NODE="1", OMIXA_WORKER_PROCESSES=str(a.processes), PYTHONPATH=ROOT)
    os.environ.update(env); sys.path.insert(0, ROOT); os.makedirs(env["OMIXA_TEMP_DIR"], exist_ok=True)
    from app import create_app
    app = create_app(); app.config["TESTING"] = True
    devices = {"A": app.test_client(), "B": app.test_client()}     # separate cookies = separate devices
    jobs = {}
    for name, seed in (("A", 1), ("B", 2)):
        r = devices[name].post("/api/upload/", data={"file": (io.BytesIO(make_csv(name, a.rows, seed)), f"{name}.csv")},
                               content_type="multipart/form-data")
        assert r.status_code == 201, r.get_json(); jobs[name] = r.get_json()["job_id"]
    for name in ("A", "B"):
        r = devices[name].post(f"/api/process/{jobs[name]}", json={"rules": ["formatting", "email_cleaning", "numeric_text_cleaning", "case_standardization", "date_standardization", "duplicates"]})
        assert r.status_code == 202, (r.status_code, r.get_json())
    t0 = time.time()
    procs = [subprocess.Popen([sys.executable, "worker.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for _ in range(a.workers)]
    seen = {n: {} for n in jobs}; owner = {}
    try:
        db = sqlite3.connect(env["OMIXA_DB_PATH"], timeout=5)
        while time.time() - t0 < a.timeout:
            for n, jid in jobs.items():
                row = db.execute("SELECT status, lease_owner FROM jobs WHERE job_id=?", (jid,)).fetchone()
                if row:
                    st = row[0]; seen[n].setdefault(st, time.time() - t0)
                    if row[1]: owner[n] = row[1]
            if all("processed" in seen[n] or "failed" in seen[n] for n in jobs): break
            time.sleep(0.05)
    finally:
        for p in procs: p.terminate()
        for p in procs: p.wait(10)
    print(f"containers={a.workers} OMIXA_WORKER_PROCESSES={a.processes} rows/job={a.rows}")
    ok = True
    for n in jobs:
        s = seen[n]; print(f"  job {n}: running@{s.get('running', float('nan')):.2f}s  processed@{s.get('processed', float('nan')):.2f}s  failed={'failed' in s}  worker={owner.get(n, '?')[-14:]}")
        ok &= "processed" in s
    if ok:
        A0, A1, B0, B1 = seen["A"]["running"], seen["A"]["processed"], seen["B"]["running"], seen["B"]["processed"]
        overlap = max(0.0, min(A1, B1) - max(A0, B0))
        print(f"  overlap of execution windows: {overlap:.2f}s  -> {'CONCURRENT' if overlap > 0.2 else 'SERIAL'}")
        print(f"  distinct workers used: {len(set(owner.values()))}")
        for n, prefix in (("A", "A_"), ("B", "B_")):
            d = devices[n].get(f"/api/download/{jobs[n]}"); assert d.status_code == 200, d.status_code
            lines = d.data.decode().splitlines()[1:]; ids = [l.split(",")[0] for l in lines]
            other = "B" if n == "A" else "A"
            print(f"  device {n} download: rows={len(ids)} all_ids_own_dataset={all(i.startswith(prefix) for i in ids)} "
                  f"cross_device_download_status={devices[other].get(f'/api/download/{jobs[n]}').status_code}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
