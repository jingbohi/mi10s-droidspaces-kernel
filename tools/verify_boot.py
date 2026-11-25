#!/usr/bin/env python3
"""Structural verification for a boot.img rebuilt by pack_boot.py.

Every check must pass; any failure exits non-zero so CI stops before publishing.
The point is to prove that only the kernel payload changed:

    header magic / version / size / total size
    kernel size and bytes          == the Image that was built
    kernel padding                 zeroed, page aligned
    ramdisk size / MD5             == the stock template
    os_version / cmdline           == the stock template
    vbmeta blob / AVB footer       == the stock template
    no KernelPatch / KernelSU data == the release is the no-root flavour

Usage:

    python3 tools/verify_boot.py --boot dist/boot-mi10s-droidspaces.img \
        --manifest bootimg/thyme-boot-template.json --image dist/Image
"""
import argparse
import hashlib
import json
import struct
import sys

PAGE = 4096
AVB_FOOTER_LEN = 64
FAILED = []


def align(n, page=PAGE):
    return (n + page - 1) // page * page


def check(ok, label, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--image", required=True)
    args = ap.parse_args()

    with open(args.manifest, encoding="utf-8") as f:
        mf = json.load(f)
    boot = open(args.boot, "rb").read()
    image = open(args.image, "rb").read()

    print(f"boot   : {args.boot} ({len(boot):,} B)")
    print(f"image  : {args.image} ({len(image):,} B)")
    print(f"md5    : {hashlib.md5(boot).hexdigest()}")
    print(f"sha256 : {hashlib.sha256(boot).hexdigest()}")
    print("checks:")

    # container: magic, version, header size, total size
    check(boot[:8] == b"ANDROID!", "boot magic ANDROID!")
    kernel_size, ramdisk_size, os_version, header_size = struct.unpack("<4I", boot[8:24])
    header_version = struct.unpack("<I", boot[40:44])[0]
    check(header_version == mf["header_version"], "header version", str(header_version))
    check(header_size == mf["header_size"], "header size", str(header_size))
    check(len(boot) == mf["total_size"], "total size", f"{len(boot):,} B")

    # kernel payload
    kernel_off = mf["kernel_offset"]
    check(kernel_size == len(image), "kernel size == Image size", f"{kernel_size:,} B")
    check(boot[kernel_off:kernel_off + kernel_size] == image, "kernel bytes == Image")
    rd_off = kernel_off + align(kernel_size)
    pad = boot[kernel_off + kernel_size:rd_off]
    check(pad == b"\x00" * len(pad), "kernel padding zeroed", f"{len(pad):,} B")

    # ramdisk untouched
    rd = boot[rd_off:rd_off + mf["ramdisk_size"]]
    check(ramdisk_size == mf["ramdisk_size"], "ramdisk size", f"{ramdisk_size:,} B")
    check(hashlib.md5(rd).hexdigest() == mf["ramdisk_md5"], "ramdisk MD5", hashlib.md5(rd).hexdigest())

    # header fields that must not change
    check(os_version == mf["os_version"], "os_version unchanged", f"0x{os_version:08x}")
    cmdline = boot[64:64 + 512].split(b"\x00")[0].decode("ascii", "replace")
    check(cmdline == mf["cmdline"], "cmdline unchanged", cmdline or "(empty)")

    # AVB blob + footer
    footer = boot[-AVB_FOOTER_LEN:]
    check(footer[:4] == b"AVBf", "AVB footer magic")
    _, _, orig_size, vbmeta_off, vbmeta_size = struct.unpack(">IIQQQ", footer[4:36])
    vbmeta = boot[vbmeta_off:vbmeta_off + vbmeta_size]
    check(vbmeta_size == mf["vbmeta_size"], "vbmeta size", f"{vbmeta_size} B")
    check(hashlib.sha256(vbmeta).hexdigest() == mf["vbmeta_sha256"], "vbmeta bytes unchanged", f"@{vbmeta_off:,}")
    check(vbmeta_off % PAGE == 0, "vbmeta page aligned", str(vbmeta_off))

    # footer body: byte-identical unless the kernel grew past the old vbmeta offset
    body = bytearray(footer)
    relocated = vbmeta_off != mf["vbmeta_offset"]
    if relocated:
        body[12:28] = b"\x00" * 16
    expected = mf["footer_sha256"] if not relocated else mf["footer_sha256_masked"]
    check(hashlib.sha256(bytes(body)).hexdigest() == expected,
          "footer body unchanged" + (" (relocated offsets masked)" if relocated else ""),
          f"vbmeta @{vbmeta_off:,}, orig_size {orig_size:,}")

    # release flavour
    check(b"KernelPatch" not in boot and b"KernelSU" not in boot and b"kpatch" not in boot,
          "no root patch payload")

    print(f"result : {'PASS' if not FAILED else 'FAIL'} ({len(FAILED)} failed)")
    if FAILED:
        for f in FAILED:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
