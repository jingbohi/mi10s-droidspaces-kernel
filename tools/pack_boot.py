#!/usr/bin/env python3
"""Rebuild a partition-sized boot.img from the layout template + a new kernel Image.

Only the kernel payload is replaced. Everything else comes from the template:

    header page  -> kernel_size rewritten, all other fields untouched
    ramdisk      -> copied byte for byte
    AVB vbmeta   -> copied byte for byte (not re-signed; unlocked bootloader)
    AVB footer   -> copied, offsets patched only if the kernel grows past vbmeta

The stock AVB hash is therefore stale in the output. That is intentional: the
bootloader is unlocked and does not enforce it for this device.

Usage:

    python3 tools/pack_boot.py \
        --template bootimg/thyme-boot-template.bin \
        --manifest bootimg/thyme-boot-template.json \
        --image dist/Image \
        --out dist/boot-mi10s-droidspaces.img
"""
import argparse
import hashlib
import json
import struct
import sys

PAGE = 4096
AVB_FOOTER_LEN = 64


def align(n, page=PAGE):
    return (n + page - 1) // page * page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.manifest, encoding="utf-8") as f:
        mf = json.load(f)
    tpl = open(args.template, "rb").read()
    image = open(args.image, "rb").read()

    if len(tpl) != mf["template_size"]:
        sys.exit(f"template size {len(tpl)} != manifest {mf['template_size']}")
    if hashlib.md5(tpl).hexdigest() != mf["template_md5"]:
        sys.exit("template MD5 does not match the manifest")

    header = bytearray(tpl[:PAGE])
    ramdisk = tpl[PAGE:PAGE + align(mf["ramdisk_size"])]
    if len(ramdisk) != align(mf["ramdisk_size"]):
        sys.exit("template is truncated (ramdisk)")
    vbmeta = tpl[PAGE + len(ramdisk):PAGE + len(ramdisk) + mf["vbmeta_size"]]
    footer = bytearray(tpl[-AVB_FOOTER_LEN:])

    if hashlib.sha256(bytes(header)).hexdigest() != mf["header_sha256"]:
        sys.exit("header does not match the manifest")
    if hashlib.md5(ramdisk[:mf["ramdisk_size"]]).hexdigest() != mf["ramdisk_md5"]:
        sys.exit("ramdisk does not match the manifest")
    if hashlib.sha256(vbmeta).hexdigest() != mf["vbmeta_sha256"]:
        sys.exit("vbmeta does not match the manifest")
    if footer[:4] != b"AVBf":
        sys.exit("AVB footer magic missing in the template")

    struct.pack_into("<I", header, 8, len(image))
    payload = bytes(header) + image + b"\x00" * (align(len(image)) - len(image)) + ramdisk
    total = mf["total_size"]
    vbmeta_off = mf["vbmeta_offset"]
    vbmeta_size = mf["vbmeta_size"]

    if len(payload) > total:
        sys.exit(f"new payload {len(payload)} does not fit in {total} bytes")

    if len(payload) <= vbmeta_off:
        out = payload + b"\x00" * (vbmeta_off - len(payload))
        out += vbmeta + b"\x00" * (total - vbmeta_off - vbmeta_size - AVB_FOOTER_LEN)
        out += bytes(footer)
    else:
        new_vbmeta_off = align(len(payload))
        if new_vbmeta_off + vbmeta_size + AVB_FOOTER_LEN > total:
            sys.exit("new kernel leaves no room for the AVB blob")
        struct.pack_into(">Q", footer, 12, len(payload))       # original_image_size
        struct.pack_into(">Q", footer, 20, new_vbmeta_off)     # vbmeta_offset
        vbmeta_off = new_vbmeta_off
        out = payload + b"\x00" * (new_vbmeta_off - len(payload))
        out += vbmeta + b"\x00" * (total - new_vbmeta_off - vbmeta_size - AVB_FOOTER_LEN)
        out += bytes(footer)

    if len(out) != total:
        sys.exit(f"output size {len(out)} != {total}")

    with open(args.out, "wb") as f:
        f.write(out)

    print(f"template: {args.template}")
    print(f"image   : {args.image} ({len(image):,} B)")
    print(f"output  : {args.out} ({len(out):,} B)")
    print(f"kernel  : {len(image):,} B at {mf['kernel_offset']:,}")
    print(f"ramdisk : {mf['ramdisk_size']:,} B, md5 {mf['ramdisk_md5']}")
    print(f"vbmeta  : @{vbmeta_off:,} ({vbmeta_size} B)")
    print(f"md5     : {hashlib.md5(out).hexdigest()}")
    print(f"sha256  : {hashlib.sha256(out).hexdigest()}")


if __name__ == "__main__":
    main()
