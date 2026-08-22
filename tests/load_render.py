"""
Load test for the certificate download path.

Answers the question section 3 of MULTI_TENANT_PLAN.md asks: can one instance serve
1000 participants arriving in the same few minutes? It drives the real route through
Flask's test client, so token verification, the render semaphore, the caches and the
encode are all inside the measurement. No network, no Supabase, no KV - the same
constraints as the assertion suites next to it.

Not named test_*: it takes minutes and asserts nothing. Run it deliberately.

    python tests/load_render.py                        # 1000 JPEG, 8 concurrent
    python tests/load_render.py --format png           # the legacy encoder
    python tests/load_render.py --concurrency 32       # a harder spike
    python tests/load_render.py --template path/to/template.png
    python tests/load_render.py --count 200 --size 1600x1100

It renders against a real template out of events/ by default, because encode cost
is dominated by image content and no cheap synthetic stands in for it: a flat fill
makes PNG look 80x cheaper than it is, and procedural noise makes JPEG look 5x more
expensive. The synthetic fallback exists so the script still runs on a checkout with
no events, and says so loudly when it is used.

Measure with --concurrency 1 to get one gunicorn worker's real rate. Threads do not
give JPEG any parallelism: Pillow releases the GIL in its file-descriptor encode
path, but the app encodes to BytesIO (it must not write to disk), and on that path
only the zlib-backed PNG encoder releases it. Measured over 16 encodes of a real
3508x2480 template, 1 thread vs 8:

    JPEG q92 -> BytesIO      381 ms -> 381 ms   1.00x   (GIL held)
    JPEG q92 -> real file    388 ms ->  74 ms   5.21x   (GIL released)
    PNG  lvl3 -> BytesIO    1887 ms -> 313 ms   6.04x   (GIL released)
    pure-Python control      650 ms -> 634 ms   1.03x

So JPEG parallelism comes from --workers (separate processes), not --threads, and
a run at --concurrency 8 understates JPEG badly. Per-worker throughput is the
number that matters for capacity planning.

Peak RSS is the process high-water mark, so it includes the interpreter and the
template cache. It is per worker: multiply by --workers for the instance total.
Compare runs, not absolute numbers.
"""
import argparse
import glob
import io
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageFilter

from _fixture import A, TEST_SLUG, setup_scratch_event, teardown_scratch


def peak_rss_bytes() -> int | None:
    """Process peak RSS from the stdlib, or None where neither source exists."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        # argtypes/restype are not optional here: GetCurrentProcess returns the
        # pseudo-handle (HANDLE)-1, and ctypes' default int restype truncates it
        # to 32 bits on a 64-bit build, so the call silently fails.
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(COUNTERS), wintypes.DWORD]

        counters = COUNTERS()
        counters.cb = ctypes.sizeof(COUNTERS)
        if not psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return int(counters.PeakWorkingSetSize)
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS reports bytes.
    return peak if sys.platform == "darwin" else peak * 1024


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_real_template() -> str | None:
    """The largest template in events/, or None on a checkout without one."""
    candidates = []
    for event in sorted(glob.glob(os.path.join(REPO_ROOT, "events", "*"))):
        for path in glob.glob(os.path.join(event, "template.*")):
            if os.path.splitext(path)[1].lower() in A.TEMPLATE_EXTENSIONS:
                candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=os.path.getsize)


def build_synthetic_template(width: int, height: int) -> Image.Image:
    """
    A stand-in template for a checkout with no events/.

    Read the numbers from a run using this with suspicion. Encode cost tracks image
    content, and nothing procedural lands in the same place as scanned or designed
    artwork: measured against the real 3508x2480 templates (PNG 2.53 MB / 188 ms,
    JPEG q92 4:4:4 0.90 MB / 24 ms), a flat fill encodes to 0.03 MB and blocky
    procedural texture to 0.06 MB, while the same noise costs JPEG 5x more than the
    real thing. This blurred texture is the closest cheap approximation found, and
    it still understates PNG by roughly 5x.
    """
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        base = (200 + 40 * y // height, 180 + 50 * y // height, 150 + 80 * y // height)
        row_seed = (y >> 4) * 40503
        for x in range(width):
            jitter = (((x >> 4) * 2654435761) ^ row_seed) & 0x0F
            pixels[x, y] = (base[0] ^ jitter, base[1] ^ jitter, base[2] ^ jitter)
    return image.filter(ImageFilter.GaussianBlur(3))


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1000,
                        help="distinct participants to render (default 1000)")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="client threads issuing requests (default 8)")
    parser.add_argument("--format", choices=("jpeg", "png"), default="jpeg",
                        help="event download_format (default jpeg)")
    parser.add_argument("--template", default=None,
                        help="template image to render against "
                             "(default: the largest one in events/)")
    parser.add_argument("--size", default="native",
                        help="resize the template to WxH, or 'native' to leave it alone")
    parser.add_argument("--render-slots", type=int, default=None,
                        help="global render slots (default: detected cores)")
    parser.add_argument("--per-tenant-slots", type=int, default=None,
                        help="per-tenant render slots (default: half the global limit)")
    args = parser.parse_args()

    resize_to = None
    if args.size.lower() != "native":
        resize_to = tuple(int(part) for part in args.size.lower().split("x"))

    scratch = setup_scratch_event()
    try:
        source = args.template or find_real_template()
        if source:
            image = Image.open(source)
            image = image.convert("RGBA" if image.mode in ("RGBA", "LA") else "RGB")
            origin = os.path.relpath(source, REPO_ROOT)
        else:
            size = resize_to or (3508, 2480)
            print(f"no template found in events/ - building a synthetic {size[0]}x{size[1]} "
                  f"one. ENCODE COSTS FROM THIS RUN ARE NOT REPRESENTATIVE; pass "
                  f"--template to measure against real artwork.", flush=True)
            image = build_synthetic_template(*size)
            origin = "synthetic"
        if resize_to and image.size != resize_to:
            image = image.resize(resize_to, Image.LANCZOS)
        image.save(os.path.join(A.EVENTS_DIR, TEST_SLUG, "template.png"), format="PNG")

        config = A.load_event(TEST_SLUG)
        config["download_format"] = args.format
        config.pop("template_version", None)
        A.save_event_config(TEST_SLUG, config)
        A._TEMPLATE_IMAGE_CACHE.clear()
        A._RENDERED_CERT_CACHE.clear()

        if args.render_slots is not None or args.per_tenant_slots is not None:
            A.configure_render_slots(args.render_slots, args.per_tenant_slots)

        template_bytes = A.decoded_image_bytes(A.get_template_image(TEST_SLUG, A.load_event(TEST_SLUG)))

        print(f"template              : {origin} {image.size} {image.mode}")
        print(f"cores detected        : {A.available_cores()}")
        print(f"render slots          : {A.RENDER_MAX_CONCURRENCY} global, "
              f"{A.RENDER_MAX_CONCURRENCY_PER_TENANT} per tenant")
        print(f"queue timeout         : {A.RENDER_QUEUE_TIMEOUT_SEC}s")
        print(f"decoded template      : {template_bytes / 1024 / 1024:.1f} MB")
        print(f"template cache budget : {A._TEMPLATE_IMAGE_CACHE.max_bytes / 1024 / 1024:.0f} MB")
        print(f"workload              : {args.count} participants, "
              f"{args.concurrency} concurrent, format={args.format}")
        print()

        tokens = [A.make_cert_token(TEST_SLUG, f"Participant Number {n:05d}")
                  for n in range(args.count)]

        local = threading.local()
        latencies: list[float] = []
        statuses: dict[int, int] = {}
        total_bytes = 0
        lock = threading.Lock()

        def fetch(token: str) -> None:
            nonlocal total_bytes
            client = getattr(local, "client", None)
            if client is None:
                # One client per thread: Flask's test client keeps a cookie jar and
                # is not built to be driven from several threads at once.
                client = local.client = A.app.test_client()
            started = time.perf_counter()
            response = client.get(f"/download-file/{token}")
            elapsed = (time.perf_counter() - started) * 1000
            size = len(response.data)
            response.close()
            with lock:
                latencies.append(elapsed)
                statuses[response.status_code] = statuses.get(response.status_code, 0) + 1
                total_bytes += size

        wall_start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            list(pool.map(fetch, tokens))
        wall = time.perf_counter() - wall_start

        ok = statuses.get(200, 0)
        busy = statuses.get(503, 0)
        print(f"wall clock            : {wall:.1f}s")
        print(f"throughput            : {args.count / wall:.1f} certificates/sec")
        print(f"statuses              : " +
              ", ".join(f"{code}x{n}" for code, n in sorted(statuses.items())))
        if busy:
            print(f"  {busy} shed as 503 - the pool was saturated, which is the "
                  f"intended behaviour, not a failure")
        print()
        print(f"latency p50           : {percentile(latencies, 0.50):.0f} ms")
        print(f"latency p95           : {percentile(latencies, 0.95):.0f} ms")
        print(f"latency p99           : {percentile(latencies, 0.99):.0f} ms")
        print(f"latency max           : {max(latencies):.0f} ms")
        print(f"latency mean          : {statistics.fmean(latencies):.0f} ms")
        print()
        if ok:
            print(f"bytes per certificate : {total_bytes / max(1, ok) / 1024:.0f} KB")
            print(f"egress for {args.count:>5}     : {total_bytes / 1024 / 1024:.0f} MB")
        peak = peak_rss_bytes()
        readable = f"{peak / 1024 / 1024:.0f} MB" if peak else "unavailable on this platform"
        print(f"peak RSS              : {readable}")

        # What this means for the section 3 target.
        cpu_per_cert = wall * args.concurrency / max(1, args.count)
        print()
        print(f"cores needed for 1000 in 10 min (1.7/s) : {cpu_per_cert * 1.7:.2f}")
        print(f"cores needed for 1000 in  1 min  (17/s) : {cpu_per_cert * 17:.2f}")
        return 0
    finally:
        teardown_scratch(scratch)


if __name__ == "__main__":
    sys.exit(main())
