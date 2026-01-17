#!/usr/bin/env python3
"""
coupon_gen.py — concurrent coupon generator with hunter-style UI.

- Concurrency per generare molti coupon rapidamente.
- UI console stile hunter.py:
    - Header con info run
    - Una riga per ogni coupon trovato
    - Una sola barra di progresso in basso (aggiornata con \r)
- Log dettagliati solo su file se usi --log.
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


# -----------------------------------------------------------------------------
# Logging / headers / parsing helpers
# -----------------------------------------------------------------------------
def setup_logger(logfile: Optional[str]):
    logger.setLevel(logging.DEBUG)

    # Console: WARNING+ only, per non sporcare la UI
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    logger.addHandler(ch)

    if logfile:
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
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
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
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


def _extract_from_webhook_response(
    r: requests.Response,
    session: requests.Session,
) -> Tuple[Optional[str], Optional[str]]:
    """Estrae codice coupon da JSON o testo, con fallback su THANKS_BASE."""
    try:
        jd = r.json()
    except Exception:
        jd = None

    if isinstance(jd, dict):
        coupon = jd.get("Coupon") or jd.get("coupon") or jd.get("code") or jd.get("CouponCode")
        page = jd.get("Page") or jd.get("page")
        mnp = jd.get("MNP") or jd.get("mnp") or jd.get("op")

        redirect_url = (
            jd.get("url")
            or jd.get("Url")
            or jd.get("URL")
            or jd.get("redirectUrl")
            or jd.get("redirect_url")
        )

        if coupon and page:
            mnp_enc = requests.utils.requote_uri(mnp) if mnp else ""
            final_url = f"https://verymobile.it/promo-meta1/{page}?code={coupon}"
            if mnp_enc:
                final_url += f"&op={mnp_enc}"
            return coupon, final_url

        if redirect_url:
            code_from_url = parse_code_from_url(redirect_url) or parse_code_from_text(str(redirect_url))
            if code_from_url:
                return code_from_url, redirect_url

        if coupon and not page and not redirect_url:
            op_val = mnp or jd.get("operator") or jd.get("op")
            final_url = f"{THANKS_BASE}?code={coupon}"
            if op_val:
                final_url += f"&op={requests.utils.requote_uri(op_val)}"
            return coupon, final_url

    body_text = r.text or ""
    code = parse_code_from_text(body_text)
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


# -----------------------------------------------------------------------------
# UI stile hunter.py
# -----------------------------------------------------------------------------
def print_header(count: int, operator: str, no_email: bool, concurrency: int):
    print("🚀 VERY META1 COUPON GENERATOR")
    print(f"🎯 Target : {count} coupon(s)")
    print(f"📧 Mode   : {'deterministic seed/index' if no_email else 'fixed email'}")
    print(f"📡 Oper.  : {operator}")
    print(f"🧵 Workers: {concurrency}")
    print("-" * 60)
    print("Coupons:")
    print("-" * 60)


def print_status(collected: int, target: int, attempts: int, start_time: float):
    """
    Barra di progresso stile hunter.py:
    🚀 24.90% |█████░░░░░░░░░░░░░░░░░░| 5/20 attempts:30 speed:6.5/s ETA:00:12:34
    """
    elapsed = max(0.001, time.time() - start_time)
    processed = max(1, collected)  # evita divisione per 0
    percent = (processed / max(1, target)) * 100 if target > 0 else 0.0
    speed = processed / elapsed
    eta_seconds = (target - processed) / speed if speed > 0 and processed < target else 0.0
    eta_str = time.strftime("%H:%M:%S", time.gmtime(eta_seconds))

    bar_len = 30
    filled = int(bar_len * percent / 100)
    bar = "█" * filled + "░" * (bar_len - filled)

    # Usiamo \r per sovrascrivere una sola riga
    status_line = (
        f"\r🚀 {percent:5.2f}% |{bar}| "
        f"{collected}/{target}  attempts:{attempts}  "
        f"speed:{speed:4.2f}/s  ETA:{eta_str}"
    )
    print(status_line, end="", flush=True)


def log_coupon(index: int, code: str, email: str):
    """
    Stile log_find di hunter.py:
    - Pulisce la riga della barra
    - Stampa una riga con icona + dati
    - La barra verrà ristampata subito dopo dal loop principale
    """
    # cancella la riga di status corrente
    print("\r" + " " * 140 + "\r", end="")
    icon = "🟢"
    print(f"{icon} [{index:03d}] {code:8s}  {email}")
    # la barra verrà ristampata dal loop principale


def do_dry_run(email: str, operator: str, count: int, no_email: bool, concurrency: int):
    print_header(count, operator, no_email, concurrency)
    for i in range(1, count + 1):
        shown = "<deterministic_email_from_seed>" if no_email else email
        print(f"[DRY] {i:03d} 🎯 {shown}  ({operator})")
    print("-" * 60)
    print("Dry-run completato. Nessuna richiesta inviata.")


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


# -----------------------------------------------------------------------------
# Concurrency helpers
# -----------------------------------------------------------------------------
class ConcurrentGenerator:
    def __init__(self, seed_hex: str, index_file: str, used_file: str, domain: str):
        self.seed_hex = seed_hex
        self.index_file = index_file
        self.used_file = used_file
        self.domain = domain
        self.lock = threading.Lock()
        self.index = read_index(index_file)

    def next_email_and_advance(self) -> str:
        with self.lock:
            cur_idx = self.index
            email = make_deterministic_email_from_seed(self.seed_hex, cur_idx, domain=self.domain)
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
    target_count: int,
    shared_state: dict,
    config: dict,
):
    session = requests.Session()
    session.headers.update(build_headers())

    while True:
        with shared_state["lock"]:
            if shared_state["done"]:
                return
            if shared_state["collected"] >= target_count:
                shared_state["done"] = True
                return
            if shared_state["attempts"] >= config["max_attempts"]:
                shared_state["done"] = True
                logger.warning("Max attempts reached (%d)", config["max_attempts"])
                return
            shared_state["attempts"] += 1

        cur_email = gen.next_email_and_advance() if no_email else config["fallback_email"]
        payload = {"email": cur_email, "operator": operator}

        try:
            r = session.post(STANDARD_WEBHOOK_URL, json=payload, timeout=config["timeout"])
            logger.debug(
                "T%d: status=%s body=%s",
                thread_id,
                r.status_code,
                (r.text or "")[:300],
            )
        except Exception as e:
            logger.warning("T%d: request error: %s", thread_id, e)
            continue

        code, final_url = _extract_from_webhook_response(r, session)
        if code:
            with shared_state["lock"]:
                if code in shared_state["seen_codes"]:
                    continue
                shared_state["seen_codes"].add(code)
                shared_state["results"].append((cur_email, code))
                shared_state["collected"] += 1
                idx = shared_state["collected"]
                log_coupon(idx, code, cur_email)
                if shared_state["collected"] >= target_count:
                    shared_state["done"] = True
                    return

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
    seed_file: str,
    index_file: str,
    used_file: str,
    domain: str,
    reuse_seed: bool,
    concurrency: int,
    max_attempts: int,
    timeout: float = 15.0,
):
    print_header(count, operator, no_email, concurrency)

    # Seed handling
    if reuse_seed:
        seed_hex = read_seed(seed_file)
        if not seed_hex:
            print("❌ Errore: seed non trovato.")
            logger.error("Requested --reuse-seed but no seed file.")
            return []
    else:
        seed_hex = generate_new_seed_hex()
        persist_new_seed(seed_file, seed_hex)

    seed_short = seed_hex[:8]

    if pathlib.Path(index_file).exists():
        idx = read_index(index_file)
    else:
        idx = find_last_index_for_seed_in_used(used_file, seed_short)
        write_index(index_file, idx)

    gen = ConcurrentGenerator(seed_hex=seed_hex, index_file=index_file, used_file=used_file, domain=domain)
    with gen.lock:
        gen.index = idx

    shared_state = {
        "lock": threading.Lock(),
        "results": [],
        "seen_codes": set(),
        "collected": 0,
        "attempts": 0,
        "done": False,
    }

    config = {
        "timeout": timeout,
        "max_attempts": max_attempts,
        "fallback_email": email,
        "per_request_delay": delay,
    }

    threads = []
    for t_id in range(concurrency):
        th = threading.Thread(
            target=worker_loop,
            args=(t_id + 1, gen, operator, no_email, count, shared_state, config),
            daemon=True,
        )
        th.start()
        threads.append(th)

    start_time = time.time()
    try:
        while True:
            time.sleep(0.3)
            with shared_state["lock"]:
                collected = shared_state["collected"]
                attempts = shared_state["attempts"]
                done = shared_state["done"]
            print_status(collected, count, attempts, start_time)
            if done:
                break
    except KeyboardInterrupt:
        logger.warning("Interrupted by user; signalling workers to stop...")
        with shared_state["lock"]:
            shared_state["done"] = True

    for th in threads:
        th.join(timeout=1.0)

    print()  # newline dopo la barra

    results_rows = shared_state["results"][:count]

    if output_csv:
        save_results_simple_csv(results_rows, output_csv)

    print("-" * 60)
    if results_rows:
        print(f"✅ Raccolti {len(results_rows)}/{count} coupon. Seed: {seed_short}")
    else:
        print("❌ Nessun coupon raccolto.")
    print("-" * 60)

    return results_rows


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
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
    p.add_argument("--count", type=int, default=1, help="Number of coupons to attempt to collect (default 1)")
    p.add_argument("--email", default="example@email.com", help="Email to use when not --no-email")
    p.add_argument("--operator", default="CoopVoce", help="Operator to send (default CoopVoce)")
    p.add_argument("--delay", type=float, default=0.0, help="Per-request polite delay in seconds (default 0)")
    p.add_argument("--max-retries", type=int, default=6, help="Legacy per-slot max retries (used to derive max_attempts)")
    p.add_argument("--max-attempts", type=int, default=0, help="Total request limit across all workers (0 => default count*max_retries*concurrency)")
    p.add_argument("--concurrency", type=int, default=6, help="Number of worker threads (default 6)")
    p.add_argument("--output", default="results.csv", help="CSV output file path (default results.csv)")
    p.add_argument("--log", default="", help="Log file path (optional). Provides detailed DEBUG logs.")
    p.add_argument("--seed-file", default=DEFAULT_SEED_FILE, help="File to persist the seed for this run (default email_seed.txt)")
    p.add_argument("--index-file", default=DEFAULT_INDEX_FILE, help="File to persist/read next index (default email_index.txt)")
    p.add_argument("--used-file", default=DEFAULT_USED_FILE, help="Append-only file with used emails (default used_emails.txt)")
    p.add_argument("--domain", default="example.com", help="Domain for generated emails (default example.com)")
    p.add_argument("--reuse-seed", action="store_true", help="Reuse existing seed in --seed-file instead of generating a new one")
    p.add_argument("--concurrency-max-attempts-multiplier", type=int, default=1, help="Multiplier used when defaulting max_attempts=count*max_retries*concurrency")
    args = p.parse_args(argv)

    setup_logger(args.log or None)

    if not args.real:
        do_dry_run(args.email, args.operator, args.count, args.no_email, args.concurrency)
        return

    if not args.yes:
        if not confirm("Are you absolutely sure you want to send real requests now?"):
            print("Operazione annullata.")
            return

    if args.max_attempts <= 0:
        max_attempts = max(
            1,
            args.count * args.max_retries * args.concurrency * args.concurrency_max_attempts_multiplier,
        )
    else:
        max_attempts = args.max_attempts

    generate_coupons_concurrent(
        email=args.email,
        operator=args.operator,
        count=args.count,
        delay=args.delay,
        output_csv=(args.output or None),
        no_email=args.no_email,
        max_retries=args.max_retries,
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