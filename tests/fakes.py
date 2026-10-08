"""In-memory stand-ins for Redis and an S3 client (only the calls Omixa makes)."""
import threading
import time


class FakeRedis:
    def __init__(self):
        self.kv, self.exp, self.z, self.l = {}, {}, {}, {}
        self.down = False
        self._lock = threading.RLock()

    def _chk(self):
        if self.down:
            raise ConnectionError("redis down")

    def _alive(self, k):
        if k in self.exp and self.exp[k] <= time.time():
            self.kv.pop(k, None); self.exp.pop(k, None)

    def ping(self): self._chk(); return True
    def incr(self, k):
        self._chk(); self._alive(k); self.kv[k] = int(self.kv.get(k, 0)) + 1; return self.kv[k]
    def expire(self, k, s): self._chk(); self.exp[k] = time.time() + s; return True
    def get(self, k): self._chk(); self._alive(k); return self.kv.get(k)
    def set(self, k, v, ex=None, nx=False):
        self._chk(); self._alive(k)
        if nx and k in self.kv: return None
        self.kv[k] = v
        if ex: self.exp[k] = time.time() + ex
        return True
    def delete(self, k): self._chk(); self.kv.pop(k, None)

    def zadd(self, name, mapping, nx=False):
        self._chk(); z = self.z.setdefault(name, {}); n = 0
        for m, sc in mapping.items():
            if nx and m in z: continue
            z[m] = sc; n += 1
        return n
    def zpopmin(self, name, count=1):
        self._chk(); z = self.z.get(name, {})
        items = sorted(z.items(), key=lambda kv: kv[1])[:count]
        for m, _ in items: z.pop(m)
        return items
    def zrem(self, name, m): self._chk(); return 1 if self.z.get(name, {}).pop(m, None) is not None else 0
    def zcard(self, name): self._chk(); return len(self.z.get(name, {}))
    def zrank(self, name, m):
        self._chk(); order = [k for k, _ in sorted(self.z.get(name, {}).items(), key=lambda kv: kv[1])]
        return order.index(m) if m in order else None
    def zrangebyscore(self, name, lo, hi, start=0, num=None):
        self._chk(); lo = float("-inf") if lo == "-inf" else lo
        return [m for m, s in sorted(self.z.get(name, {}).items(), key=lambda kv: kv[1]) if lo <= s <= hi][start:(start + num) if num else None]
    def lpush(self, name, v): self._chk(); self.l.setdefault(name, []).insert(0, v)
    def ltrim(self, name, a, b): self._chk(); self.l[name] = self.l.get(name, [])[a:b + 1]

    def pipeline(self):
        outer = self
        class P:
            def __init__(s): s.ops = []
            def incr(s, k): s.ops.append(("incr", (k,))); return s
            def expire(s, k, t): s.ops.append(("expire", (k, t))); return s
            def execute(s): return [getattr(outer, n)(*a) for n, a in s.ops]
        return P()


class FakeS3Client:
    """Records calls; stores bytes in memory."""
    def __init__(self):
        self.objects, self.calls = {}, []

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
        self.calls.append(("upload_fileobj", key, ExtraArgs)); self.objects[(bucket, key)] = fileobj.read()

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.calls.append(("upload_file", key, ExtraArgs))
        with open(path, "rb") as f: self.objects[(bucket, key)] = f.read()

    def download_file(self, bucket, key, dest):
        with open(dest, "wb") as f: f.write(self.objects[(bucket, key)])

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects: raise KeyError(Key)
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key): self.objects.pop((Bucket, Key), None)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.calls.append(("presign", Params["Key"], ExpiresIn))
        return f"https://signed.example/{Params['Key']}?exp={ExpiresIn}"
