#!/usr/bin/env python3
"""
coupon_gen.py — concurrent version

Adds concurrency to speed up generation of many coupons.

Main changes:
- New CLI arg: --concurrency N (default 6) to run N worker threads in parallel.
- New CLI arg: --max-attempts TOTAL to cap total attempted requests across all workers
  (default count * max_retries).
- Thread-safe index/email generation and used_emails persistence to avoid duplicates across runs.
- Workers continuously request new deterministic emails (seed+index) and stop when we collect
  the requested number of unique coupons or when max-attempts is reached.
- CSV output remains EMAIL,COUPON (two columns).
- Keeps existing flags: --real, --yes, --no-email, --retry-with-email, --count, --operator,
  --max-retries, --output, --log, --seed-file, --index-file, --used-file, --domain, --reuse-seed, --delay.

Notes:
- requests.Session is created per worker (requests Sessions are not universally thread-safe).
- File writes (index, used_emails) are done under a lock to avoid races.
- The concurrency model trades per-slot deterministic max_retries for a global max-attempts cap;
  this is more efficient when requesting many coupons.
- If you still want strict per-slot max_retries logic, I can adapt the design.

Example:
  Dry-run:
    python3 coupon_gen.py --count 3 --no-email
  Real concurrent run:
    python3 coupon_gen.py --real --yes --count 200 --no-email --concurrency 12 --output results.csv --log run.log

"""
from __future__ import annotations
import argparse
import csv
import datetime
import logging
import pathlib
import random
import threading
import time
from typing import List, Optional, Tuple

import requests

# Endpoints (from captured JS)
STANDARD_WEBHOOK_URL = "https://n8nanitia.app.n8n.cloud/webhook/3a37b494-c4b8-40e8-9404-654b21dc6a1c"
WINDTRE_WEBHOOK_URL_CTA = "https://n8nanitia.app.n8n.cloud/webhook/w3-offer"
WINDTRE_REDIRECT_BASE_URL_CTA = "https://verymobile.it/offerte/verysocial?token="
THANKS_BASE = "https://verymobile.it/promo-meta1/grazie"
PAGE_BASE = "https://verymobile.it/promo-meta1"

DEFAULT_SEED_FILE = "email_seed.txt"
DEFAULT_INDEX_FILE = "email_index.txt"
DEFAULT_USED_FILE = "used_emails.txt"
SEEDS_HISTORY_FILE = "seeds_history.txt"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")

CODE_RE_URL = __import__("re").compile(r"[?&]code=([A-Za-z0-9\-\_]+)")
CODE_RE_BODY = __import__("re").compile(r"\b([A-Z]{1,2}-[A-Z0-9]{5,8})\b")

logger = logging.getLogger("coupon_gen")


def setup_logger(logfile: Optional[str]):
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    if logfile:
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.info("Detailed logging to file: %s", logfile)


def build_headers() -> dict:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": PAGE_BASE,
        "Content-Type": "application/json",
    }


def parse_code_from_url(u: str) -> Optional[str]:
    if not u:
        return None
    m = CODE_RE_URL.search(u)
    return m.group(1) if m else None


def parse_code_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = CODE_RE_BODY.search(text)
    if m:
        return m.group(1)
    m2 = __import__("re").search(r"code[:=]\s*['\"]?([A-Za-z0-9\-\_]+)", text)
    return m2.group(1) if m2 else None


def generate_new_seed_hex() -> str:
    val = random.getrandbits(64)
    return f"{val:016x}"


def persist_new_seed(seed_file: str, seed_hex: str):
    pathlib.Path(seed_file).write_text(seed_hex + "\n", encoding="utf-8")
    ts = datetime.datetime.utcnow().isoformat() + "Z"
    with open(SEEDS_HISTORY_FILE, "a", encoding="utf-8") as fh:
        fh.write(f"{ts} {seed_hex}\n")
    logger.info("Saved new seed to %s and appended to %s", seed_file, SEEDS_HISTORY_FILE)


def read_seed(seed_file: str) -> Optional[str]:
    pf = pathlib.Path(seed_file)
    if pf.exists():
        return pf.read_text(encoding="utf-8").strip()
    return None


def read_index(index_file: str) -> int:
    pf = pathlib.Path(index_file)
    if pf.exists():
        try:
            return max(0, int(pf.read_text(encoding="utf-8").strip()))
        except Exception:
            return 0
    return 0


def write_index(index_file: str, index_value: int):
    pathlib.Path(index_file).write_text(str(index_value), encoding="utf-8")


def append_used_email(used_file: str, email: str):
    with open(used_file, "a", encoding="utf-8") as f:
        f.write(email + "\n")


def find_last_index_for_seed_in_used(used_file: str, seed_short: str) -> int:
    pf = pathlib.Path(used_file)
    if not pf.exists():
        return 0
    max_idx = -1
    with open(pf, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            local = line.split("@", 1)[0]
            if local.startswith(seed_short + "-"):
                suffix = local[len(seed_short) + 1 :]
                try:
                    v = int(suffix)
                    if v > max_idx:
                        max_idx = v
                except Exception:
                    continue
    return max_idx + 1 if max_idx >= 0 else 0


def make_deterministic_email_from_seed(seed_hex: str, index: int, domain: str = "example.com") -> str:
    seed_short = seed_hex[:8]
    return f"{seed_short}-{index:06d}@{domain}"


def _extract_from_webhook_response(r: requests.Response, session: requests.Session) -> Tuple[Optional[str], Optional[str]]:
    try:
        jd = r.json()
        if isinstance(jd, dict):
            coupon = jd.get("Coupon") or jd.get("coupon") or jd.get("code") or jd.get("CouponCode")
            page = jd.get("Page") or jd.get("page")
            mnp = jd.get("MNP") or jd.get("mnp") or jd.get("op")
            if coupon and page:
                mnp_enc = requests.utils.requote_uri(mnp) if mnp else ""
                final_url = f"https://verymobile.it/promo-meta1/{page}?code={coupon}"
                if mnp_enc:
                    final_url += f"&op={mnp_enc}"
                return coupon, final_url
    except Exception:
        pass
    code = parse_code_from_text(r.text or "")
    if code:
        return code, None
    try:
        g = session.get(THANKS_BASE, timeout=12, allow_redirects=True)
        final_url = g.url
        code2 = parse_code_from_url(final_url) or parse_code_from_text(g.text or "")
        if code2:
            return code2, final_url
    except Exception:
        pass
    return None, None


def do_dry_run(email: str, operator: str, count: int, no_email: bool):
    logger.info("DRY-RUN (no requests).")
    for i in range(1, count + 1):
        logger.info("-- Generation #%d", i)
        if operator.upper() == "WINDTRE":
            if no_email:
                logger.info("Would POST %s JSON -> {}  (no email)", WINDTRE_WEBHOOK_URL_CTA)
            else:
                logger.info("Would POST %s JSON -> {'email': '%s'}", WINDTRE_WEBHOOK_URL_CTA, email)
            logger.info("Expect JSON {'token': '...'} -> redirect %s<token>", WINDTRE_REDIRECT_BASE_URL_CTA)
        else:
            if no_email:
                logger.info("Would POST %s JSON -> {'operator':'%s'}  (no email)", STANDARD_WEBHOOK_URL, operator)
            else:
                logger.info("Would POST %s JSON -> {'email':'%s','operator':'%s'}", STANDARD_WEBHOOK_URL, email, operator)
            logger.info("Expect JSON with Coupon/Page/MNP -> redirect to https://verymobile.it/promo-meta1/{Page}?code={Coupon}&op={MNP}")


def save_results_simple_csv(rows: List[Tuple[str, str]], outpath: str):
    try:
        with open(outpath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["EMAIL", "COUPON"])
            for email, coupon in rows:
                writer.writerow([email, coupon])
        logger.info("Saved output CSV (EMAIL,COUPON) to %s", outpath)
    except Exception as e:
        logger.error("Failed to write CSV %s: %s", outpath, e)


class ConcurrentGenerator:
    def __init__(
        self,
        seed_hex: str,
        index_file: str,
        used_file: str,
        domain: str,
    ):
        self.seed_hex = seed_hex
        self.index_file = index_file
        self.used_file = used_file
        self.domain = domain
        self.lock = threading.Lock()
        # load index (file may already exist and be current)
        self.index = read_index(index_file)

    def next_email_and_advance(self) -> str:
        """
        Thread-safe: returns deterministic email for current index, writes index file and appends used_emails.
        """
        with self.lock:
            cur_idx = self.index
            email = make_deterministic_email_from_seed(self.seed_hex, cur_idx, domain=self.domain)
            # increment and persist
            self.index += 1
            try:
                write_index(self.index_file, self.index)
            except Exception:
                pass
            try:
                append_used_email(self.used_file, email)
            except Exception:
                pass
            return email


def worker_loop(
    thread_id: int,
    gen: ConcurrentGenerator,
    operator: str,
    no_email: bool,
    retry_with_email: bool,
    target_count: int,
    shared_state: dict,
    config: dict,
):
    """
    Worker thread: pulls deterministic emails from gen and attempts requests until
    shared_state['done'] or shared_state attempts exceed max_attempts or we reached target_count.
    """
    session = requests.Session()
    session.headers.update(build_headers())
    while True:
        with shared_state["lock"]:
            if shared_state["done"]:
                logger.debug("T%d: stopping because done flag", thread_id)
                return
            if shared_state["collected"] >= target_count:
                shared_state["done"] = True
                logger.debug("T%d: target reached, setting done", thread_id)
                return
            if shared_state["attempts"] >= config["max_attempts"]:
                shared_state["done"] = True
                logger.debug("T%d: max attempts reached, setting done", thread_id)
                return
            # reserve attempt slot
            shared_state["attempts"] += 1
        # get next email
        if no_email:
            cur_email = gen.next_email_and_advance()
            payload = {"email": cur_email, "operator": operator}
            url = STANDARD_WEBHOOK_URL
        else:
            cur_email = config["fallback_email"]
            payload = {"email": cur_email, "operator": operator}
            url = STANDARD_WEBHOOK_URL

        logger.debug("T%d: request payload -> %s", thread_id, payload)
        try:
            r = session.post(url, json=payload, timeout=config["timeout"])
            logger.debug("T%d: response status=%s text_snippet=%s", thread_id, r.status_code, (r.text or "")[:300])
        except Exception as e:
            logger.warning("T%d: request error: %s", thread_id, e)
            # continue to next loop; attempt already counted
            continue

        code, final_url = _extract_from_webhook_response(r, session)
        if code:
            with shared_state["lock"]:
                if code in shared_state["seen_codes"]:
                    logger.debug("T%d: got duplicate code %s; ignoring", thread_id, code)
                else:
                    # record
                    shared_state["seen_codes"].add(code)
                    shared_state["results"].append((cur_email if cur_email else "<empty>", code))
                    shared_state["collected"] += 1
                    logger.info("T%d: collected #%d code=%s email=%s", thread_id, shared_state["collected"], code, cur_email or "<empty>")
                    if shared_state["collected"] >= target_count:
                        shared_state["done"] = True
                        return
        # small optional delay for politeness
        if config.get("per_request_delay", 0):
            time.sleep(config["per_request_delay"])


def generate_coupons_concurrent(
    email: str,
    operator: str,
    count: int,
    delay: float,
    output_csv: Optional[str],
    no_email: bool,
    max_retries: int,
    retry_with_email: bool,
    seed_file: str,
    index_file: str,
    used_file: str,
    domain: str,
    reuse_seed: bool,
    concurrency: int,
    max_attempts: int,
    timeout: float = 15.0,
):
    # Seed handling
    if reuse_seed:
        seed_hex = read_seed(seed_file)
        if not seed_hex:
            logger.error("Requested --reuse-seed but seed file not found: %s", seed_file)
            return []
        logger.info("Reusing existing seed from %s: %s", seed_file, seed_hex)
    else:
        seed_hex = generate_new_seed_hex()
        persist_new_seed(seed_file, seed_hex)

    seed_short = seed_hex[:8]
    # determine starting index
    if pathlib.Path(index_file).exists():
        idx = read_index(index_file)
        logger.info("Loaded index from %s: %d", index_file, idx)
    else:
        idx = find_last_index_for_seed_in_used(used_file, seed_short)
        logger.info("Calculated starting index for seed %s from %s: %d", seed_short, used_file, idx)
        write_index(index_file, idx)

    gen = ConcurrentGenerator(seed_hex=seed_hex, index_file=index_file, used_file=used_file, domain=domain)
    # Ensure generator index is aligned with idx we computed
    with gen.lock:
        gen.index = idx

    # Shared state across threads
    shared_state = {
        "lock": threading.Lock(),
        "results": [],  # list of (email, code)
        "seen_codes": set(),
        "collected": 0,
        "attempts": 0,
        "done": False,
    }

    # config for workers
    config = {
        "timeout": timeout,
        "max_attempts": max_attempts,
        "fallback_email": email,
        "per_request_delay": delay,
    }

    # start worker threads
    threads = []
    for t_id in range(concurrency):
        th = threading.Thread(
            target=worker_loop,
            args=(t_id + 1, gen, operator, no_email, retry_with_email, count, shared_state, config),
            daemon=True,
        )
        th.start()
        threads.append(th)
    # monitor
    try:
        while True:
            time.sleep(0.5)
            with shared_state["lock"]:
                if shared_state["done"]:
                    break
    except KeyboardInterrupt:
        logger.info("Interrupted by user; signalling workers to stop...")
        with shared_state["lock"]:
            shared_state["done"] = True

    # join threads
    for th in threads:
        th.join(timeout=1.0)

    results_rows = shared_state["results"][:count]
    # If we didn't collect enough and retry_with_email requested, attempt fallback single-threaded retries
    if len(results_rows) < count and retry_with_email:
        logger.info("Not enough unique coupons collected (%d/%d). Performing single-threaded fallback retries with provided email.", len(results_rows), count)
        session = requests.Session()
        session.headers.update(build_headers())
        while len(results_rows) < count:
            try:
                r = session.post(STANDARD_WEBHOOK_URL, json={"email": email, "operator": operator}, timeout=timeout)
            except Exception as e:
                logger.error("Fallback request error: %s", e)
                break
            code, final_url = _extract_from_webhook_response(r, session)
            if code and code not in {c for (_, c) in results_rows}:
                results_rows.append((email, code))
                logger.info("Fallback found unique code %s", code)
            else:
                logger.warning("Fallback attempt did not produce new code, stopping.")
                break

    # save CSV
    if output_csv:
        save_results_simple_csv(results_rows, output_csv)

    # summary
    logger.info("Summary:")
    for i, (em, co) in enumerate(results_rows, start=1):
        logger.info("  #%d: email=%s coupon=%s", i, em, co)
    logger.info("Final seed (short) used: %s", seed_short)
    return results_rows


def confirm(prompt: str) -> bool:
    try:
        ans = input(prompt + " [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def main(argv: Optional[list] = None):
    p = argparse.ArgumentParser(description="coupon_gen.py — concurrent coupon generation (EMAIL,COUPON)")
    p.add_argument("--real", action="store_true", help="Perform real HTTP requests (default dry-run)")
    p.add_argument("--yes", action="store_true", help="Bypass interactive confirmation (use with --real)")
    p.add_argument("--no-email", action="store_true", help="Generate deterministic emails from seed/index for each attempt")
    p.add_argument("--retry-with-email", action="store_true", help="If not enough unique codes collected, try fallback with provided email")
    p.add_argument("--count", type=int, default=1, help="Number of coupons to attempt to collect (default 1)")
    p.add_argument("--email", default="example@email.com", help="Email to use when not --no-email or for retry")
    p.add_argument("--operator", default="CoopVoce", help="Operator to send (default CoopVoce)")
    p.add_argument("--delay", type=float, default=0.0, help="Per-request polite delay in seconds (default 0)")
    p.add_argument("--max-retries", type=int, default=6, help="Legacy per-slot max retries (not enforced by concurrent mode)")
    p.add_argument("--max-attempts", type=int, default=0, help="Total request limit across all workers (0 => default count*max_retries)")
    p.add_argument("--concurrency", type=int, default=6, help="Number of worker threads (default 6)")
    p.add_argument("--output", default="results.csv", help="CSV output file path (default results.csv). Will contain columns: EMAIL,COUPON")
    p.add_argument("--log", default="", help="Log file path (optional). Provides detailed DEBUG logs.")
    p.add_argument("--seed-file", default=DEFAULT_SEED_FILE, help="File to persist the seed for this run (default email_seed.txt). Overwritten each run unless --reuse-seed is passed.")
    p.add_argument("--index-file", default=DEFAULT_INDEX_FILE, help="File to persist/read next index (default email_index.txt)")
    p.add_argument("--used-file", default=DEFAULT_USED_FILE, help="Append-only file with used emails (default used_emails.txt)")
    p.add_argument("--domain", default="example.com", help="Domain to use for generated emails (default example.com)")
    p.add_argument("--reuse-seed", action="store_true", help="Reuse existing seed in --seed-file instead of generating a new one (use with care)")
    p.add_argument("--concurrency-max-attempts-multiplier", type=int, default=1, help="Multiplier used when defaulting max_attempts=count*max_retries (default 1)")
    args = p.parse_args(argv)

    setup_logger(args.log or None)

    if not args.real:
        do_dry_run(args.email, args.operator, args.count, args.no_email)
        return

    logger.info("REAL mode selected. You confirmed earlier that you have authorization from your employer.")
    if not args.yes:
        if not confirm("Are you absolutely sure you want to send real requests now?"):
            logger.info("Aborted by user.")
            return
    else:
        logger.info("--yes provided: skipping interactive confirmation.")

    # derive max attempts if not set
    if args.max_attempts <= 0:
        max_attempts = max(1, args.count * args.max_retries * args.concurrency * args.concurrency_max_attempts_multiplier)
    else:
        max_attempts = args.max_attempts

    logger.info("Starting concurrent run: count=%d concurrency=%d max_attempts=%d", args.count, args.concurrency, max_attempts)

    generate_coupons_concurrent(
        email=args.email,
        operator=args.operator,
        count=args.count,
        delay=args.delay,
        output_csv=(args.output or None),
        no_email=args.no_email,
        max_retries=args.max_retries,
        retry_with_email=args.retry_with_email,
        seed_file=args.seed_file,
        index_file=args.index_file,
        used_file=args.used_file,
        domain=args.domain,
        reuse_seed=args.reuse_seed,
        concurrency=args.concurrency,
        max_attempts=max_attempts,
    )


if __name__ == "__main__":
    main()