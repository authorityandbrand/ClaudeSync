#!/usr/bin/env python3
"""
r2-agent-context.py — Build an agent context bundle from R2 files.

Usage:
  python3 r2-agent-context.py r2://bucket/prefix [--ext .pdf .txt .md] [--out context.txt]

Downloads files from R2, reads text content, and emits a single
concatenated context document an agent can ingest. Binary files
(PDFs, images) are listed but not inlined.
"""

import os
import sys
import argparse
import tempfile
from pathlib import Path

import boto3
from botocore.config import Config


def get_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["jurisdiction"],
        aws_access_key_id=os.environ["Access_Key_ID"],
        aws_secret_access_key=os.environ["Secret_Access_Key"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


TEXT_EXTENSIONS = {".txt", ".md", ".json", ".csv", ".xml", ".html", ".py",
                   ".js", ".ts", ".sh", ".yaml", ".yml", ".toml", ".rst",
                   ".log", ".sql", ".r2", ".email", ".eml"}


def is_text(key, allowed_ext):
    ext = Path(key).suffix.lower()
    if allowed_ext:
        return ext in {e.lower() for e in allowed_ext}
    return ext in TEXT_EXTENSIONS


def parse_r2_path(path):
    path = path.lstrip("r2://").lstrip("/")
    parts = path.split("/", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def list_objects(client, bucket, prefix):
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    while True:
        resp = client.list_objects_v2(**kwargs)
        yield from resp.get("Contents", [])
        if not resp.get("IsTruncated"):
            break
        kwargs["ContinuationToken"] = resp["NextContinuationToken"]


def main():
    parser = argparse.ArgumentParser(description="Build agent context from R2 files")
    parser.add_argument("path", help="r2://bucket[/prefix]")
    parser.add_argument("--ext", nargs="+", metavar="EXT", help="File extensions to include (e.g. .txt .md)")
    parser.add_argument("--out", default="-", help="Output file (default: stdout)")
    parser.add_argument("--max-size", type=int, default=512*1024,
                        help="Max individual file size in bytes to inline (default: 512KB)")
    args = parser.parse_args()

    client = get_client()
    bucket, prefix = parse_r2_path(args.path)

    out = open(args.out, "w") if args.out != "-" else sys.stdout

    objects = list(list_objects(client, bucket, prefix))
    print(f"# Agent Context Bundle", file=out)
    print(f"# Source: r2://{bucket}/{prefix}", file=out)
    print(f"# Files: {len(objects)}", file=out)
    print(f"# Generated: {__import__('datetime').datetime.utcnow().isoformat()}Z\n", file=out)

    skipped = []
    for obj in sorted(objects, key=lambda o: o["Key"]):
        key = obj["Key"]
        size = obj.get("Size", 0)

        print(f"\n{'='*70}", file=out)
        print(f"FILE: r2://{bucket}/{key}  ({size:,} bytes)", file=out)
        print(f"{'='*70}", file=out)

        if not is_text(key, args.ext):
            print(f"[Binary file — not inlined]", file=out)
            skipped.append(key)
            continue

        if size > args.max_size:
            print(f"[File too large ({size:,} bytes > {args.max_size:,}) — not inlined]", file=out)
            skipped.append(key)
            continue

        try:
            data = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            print(data.decode("utf-8", errors="replace"), file=out)
        except Exception as e:
            print(f"[Error reading file: {e}]", file=out)

    if skipped:
        print(f"\n{'='*70}", file=out)
        print(f"# {len(skipped)} files skipped (binary or too large):", file=out)
        for k in skipped:
            print(f"#   r2://{bucket}/{k}", file=out)

    if args.out != "-":
        out.close()
        print(f"Context written to: {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
