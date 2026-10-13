"""CLI entry: `fakescan-lab` — arm faults / loopback for a live fakescan."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .client import DEFAULT_BASE_URL, Client


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fakescan-lab", description="fakescan lab harness")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="GET /health")
    sub.add_parser("status", help="GET /v1/status (JSON keys of interest)")
    sub.add_parser("reset", help="POST /v1/reset")

    prof = sub.add_parser("profile", help="POST /v1/profile — switch capability profile")
    prof.add_argument(
        "id",
        help="profile id (brother-wsd, wia-lossless, accept-dib, …)",
    )

    pages = sub.add_parser("pages", help="POST /v1/pages")
    pages.add_argument("count", type=int)
    pages.add_argument("--side", choices=("front", "back"))
    pages.add_argument("--order", choices=("forward", "reverse"))

    pattern = sub.add_parser("pattern", help="POST /v1/pattern (clears loopback)")
    pattern.add_argument(
        "id",
        choices=(
            "page_id",
            "wedges",
            "patches",
            "slant_f",
            "corners",
            "duplex_pair",
            "qr",
            "wia_plain",
        ),
    )

    scenario = sub.add_parser("scenario", help="POST /v1/scenario (clears loopback)")
    scenario.add_argument(
        "id",
        choices=("order", "qr", "tone", "color", "orient", "crop", "duplex"),
    )

    fault = sub.add_parser("fault", help="POST /v1/fault")
    fault.add_argument("--jam", type=_bool, default=None)
    fault.add_argument("--cover-open", type=_bool, default=None)
    fault.add_argument("--multi-feed", type=_bool, default=None)
    fault.add_argument("--lamp-error", type=_bool, default=None)
    fault.add_argument("--busy", type=_bool, default=None)
    fault.add_argument("--offline", type=_bool, default=None)
    fault.add_argument("--ms", type=int, default=None)
    fault.add_argument("--jam-after-n", type=int, default=None)
    fault.add_argument("--empty-mid-job", type=int, default=None)
    fault.add_argument("--offline-after-n", type=int, default=None)
    fault.add_argument("--multi-feed-after-n", type=int, default=None)
    fault.add_argument("--abort-retrieve", type=_bool, default=None)
    fault.add_argument("--abort-retrieve-after-n", type=int, default=None)
    fault.add_argument("--abort-retrieve-bytes", type=int, default=None)
    fault.add_argument("--lamp-warmup-ms", type=int, default=None)

    lb = sub.add_parser("loopback", help="upload files into hard loopback store")
    lb.add_argument("files", nargs="+", type=Path)
    sub.add_parser("loopback-clear", help="DELETE /v1/loopback")

    acq = sub.add_parser(
        "loopback-soap",
        help="upload files, SOAP acquire, assert byte-identical JFIF (no WIA)",
    )
    acq.add_argument("files", nargs="+", type=Path)

    dib = sub.add_parser(
        "acquire-dib",
        help="SOAP platen dib → raw BMP file (no JFIF; no WIA). Uses accept-dib unless --keep-profile.",
    )
    dib.add_argument(
        "--out",
        type=Path,
        default=Path("data/fakescan-lab/platen-dib.bmp"),
        help="output BMP path",
    )
    dib.add_argument("--dpi", type=int, default=200)
    dib.add_argument(
        "--keep-profile",
        action="store_true",
        help="do not switch to accept-dib (profile must already advertise dib)",
    )
    dib.add_argument(
        "--restore-profile",
        default=None,
        metavar="ID",
        help="profile id after acquire (default: previous; pass empty string to leave accept-dib)",
    )

    wia = sub.add_parser(
        "loopback-wia",
        help="upload files, optional pdfcraft-cli feeder scan, compare (needs WIA install)",
    )
    wia.add_argument("files", nargs="+", type=Path)
    wia.add_argument(
        "--pdfcraft-cli",
        dest="pdfcraft_cli",
        default=None,
        help="path to pdfcraft-cli (or set on PATH)",
    )
    wia.add_argument(
        "--out",
        type=Path,
        default=None,
        help="acquire output directory (default data/fakescan-lab/loopback-wia)",
    )
    wia.add_argument("--dpi", type=int, default=100)
    wia.add_argument(
        "--compare",
        choices=("auto", "exact", "pixels"),
        default="auto",
        help="exact=bytes; pixels=Pillow; auto=exact then pixels (WIA re-encodes)",
    )
    wia.add_argument(
        "--arm-only",
        action="store_true",
        help="only POST /v1/loopback; skip pdfcraft-cli",
    )

    args = p.parse_args(argv)
    with Client(args.base_url) as c:
        if args.cmd == "health":
            ok = c.health()
            print("ok" if ok else "fail")
            return 0 if ok else 1
        if args.cmd == "profile":
            st = c.set_profile(args.id)
            print(
                f"profile={st.profile_id} refuse_unknown={st.raw.get('refuse_unknown')} "
                f"formats={','.join(st.formats)}"
            )
            return 0
        if args.cmd == "status":
            st = c.status()
            print(
                f"profile={st.profile_id} pages={st.pages_remaining} "
                f"jam={st.jam} jam_after_n={st.jam_after_n} "
                f"loopback={st.loopback_pages} pattern={st.image_pattern}"
            )
            if st.loopback_digests:
                for i, d in enumerate(st.loopback_digests):
                    print(f"  digest[{i}]={d}")
            return 0
        if args.cmd == "reset":
            c.reset()
            print("reset")
            return 0
        if args.cmd == "pages":
            st = c.set_pages(args.count, side=args.side, order=args.order)
            print(
                f"pages_remaining={st.pages_remaining} "
                f"side={st.page_side} order={st.page_order}"
            )
            return 0
        if args.cmd == "pattern":
            st = c.set_pattern(args.id)
            print(f"pattern={st.image_pattern} loopback={st.loopback_pages}")
            return 0
        if args.cmd == "scenario":
            st = c.set_scenario(args.id)
            print(
                f"scenario pattern={st.image_pattern} pages={st.pages_remaining} "
                f"loopback={st.loopback_pages}"
            )
            return 0
        if args.cmd == "fault":
            st = c.set_fault(
                jam=args.jam,
                cover_open=args.cover_open,
                multi_feed=args.multi_feed,
                lamp_error=args.lamp_error,
                busy=args.busy,
                offline=args.offline,
                ms=args.ms,
                jam_after_n=args.jam_after_n,
                empty_mid_job=args.empty_mid_job,
                offline_after_n=args.offline_after_n,
                multi_feed_after_n=args.multi_feed_after_n,
                abort_retrieve=args.abort_retrieve,
                abort_retrieve_after_n=args.abort_retrieve_after_n,
                abort_retrieve_bytes=args.abort_retrieve_bytes,
                lamp_warmup_ms=args.lamp_warmup_ms,
            )
            print(
                f"jam={st.jam} cover_open={st.cover_open} multi_feed={st.multi_feed} "
                f"offline={st.offline} jam_after_n={st.jam_after_n} "
                f"empty_mid_job={st.empty_mid_job} offline_after_n={st.offline_after_n} "
                f"multi_feed_after_n={st.multi_feed_after_n} "
                f"abort_retrieve={st.abort_retrieve} "
                f"abort_retrieve_after_n={st.abort_retrieve_after_n}"
            )
            return 0
        if args.cmd == "loopback":
            c.reset()
            info = c.set_loopback_files(args.files)
            print(f"loopback count={info.count} active={info.active}")
            for i, d in enumerate(info.digests):
                print(f"  digest[{i}]={d}")
            return 0
        if args.cmd == "loopback-clear":
            c.clear_loopback()
            print("loopback cleared")
            return 0
        if args.cmd == "loopback-soap":
            from .soap import acquire_jfif_pages

            c.reset()
            originals = [f.read_bytes() for f in args.files]
            info = c.set_loopback_bytes(originals)
            print(f"armed loopback n={info.count}")
            got = acquire_jfif_pages(args.base_url, page_count=len(originals))
            if len(got) != len(originals):
                print(
                    f"page count mismatch: got {len(got)} want {len(originals)}",
                    file=sys.stderr,
                )
                return 1
            for i, (a, o) in enumerate(zip(got, originals, strict=True)):
                if a != o:
                    print(
                        f"page {i}: bytes differ (len got={len(a)} want={len(o)})",
                        file=sys.stderr,
                    )
                    return 1
                print(f"page {i}: exact match ({len(a)} bytes)")
            return 0
        if args.cmd == "acquire-dib":
            return _cmd_acquire_dib(c, args)
        if args.cmd == "loopback-wia":
            return _cmd_loopback_wia(c, args)
    return 2


def _cmd_acquire_dib(c: Client, args: argparse.Namespace) -> int:
    from .soap import acquire_dib_platen

    prev = c.status().profile_id
    if not args.keep_profile:
        st = c.set_profile("accept-dib")
        print(f"profile {prev} -> {st.profile_id} formats={','.join(st.formats)}")
    try:
        bmp = acquire_dib_platen(args.base_url, dpi=args.dpi)
    finally:
        if not args.keep_profile:
            # None → previous profile; "" → leave accept-dib; else explicit id.
            if args.restore_profile is None:
                restore = prev
            else:
                restore = str(args.restore_profile).strip()
            if restore:
                try:
                    st = c.set_profile(restore)
                    print(f"restored profile={st.profile_id}")
                except Exception as e:  # noqa: BLE001 — lab CLI
                    print(f"restore profile {restore!r} failed: {e}", file=sys.stderr)
    if not bmp.startswith(b"BM"):
        print(f"not a BMP (magic={bmp[:2]!r} len={len(bmp)})", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(bmp)
    print(f"wrote {args.out} ({len(bmp)} bytes) dpi={args.dpi} wire=dib")
    return 0


def _cmd_loopback_wia(c: Client, args: argparse.Namespace) -> int:
    try:
        from pdfcraft_fakescan.driver import (
            compare_acquired_to_originals,
            default_out_dir,
            list_scanners,
            pick_lab_wsd_device,
            resolve_cli,
            scan_feeder,
        )
    except ImportError:
        print(
            "loopback-wia needs pdfcraft-fakescan: pip install -e ../pdfcraft-fakescan",
            file=sys.stderr,
        )
        return 1

    c.reset()
    originals = [f.read_bytes() for f in args.files]
    info = c.set_loopback_bytes(originals)
    print(f"armed loopback n={info.count}")
    for i, d in enumerate(info.digests):
        print(f"  digest[{i}]={d}")
    if args.arm_only:
        print("arm-only: skip pdfcraft-cli")
        return 0

    try:
        exe = resolve_cli(args.pdfcraft_cli)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    if exe is None:
        print(
            "pdfcraft-cli not on PATH; pass --pdfcraft-cli PATH or use --arm-only",
            file=sys.stderr,
        )
        return 1

    scanners = list_scanners(exe)
    device = pick_lab_wsd_device(scanners)
    if not device:
        print(
            "no fakescan WSD device in `pdfcraft-cli scan devices` "
            "(install via scripts/fakescan.ps1 install first)",
            file=sys.stderr,
        )
        return 1
    print(f"device={device}")

    out = args.out or default_out_dir()
    try:
        files = scan_feeder(
            exe,
            out_dir=out,
            device=device,
            pages=len(originals),
            dpi=args.dpi,
        )
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    if not files:
        print(f"no acquired files under {out}", file=sys.stderr)
        return 1
    try:
        compare_acquired_to_originals(files, originals, mode=args.compare)
    except AssertionError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"ok: {len(files)} page(s) under {out}")
    return 0


def _bool(s: str) -> bool:
    v = s.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected bool, got {s!r}")


if __name__ == "__main__":
    raise SystemExit(main())
