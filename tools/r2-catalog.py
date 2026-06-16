#!/usr/bin/env python3
"""
r2-catalog.py — Build and store a full catalog of all R2 buckets and objects.

Usage:
  python3 r2-catalog.py [--out catalog.json] [--upload] [--text]

Options:
  --out FILE     Write catalog to FILE (default: /tmp/r2-catalog.json)
  --upload       Upload catalog to r2://claude/catalog/ after building
  --text         Also write a human-readable text summary
  --bucket NAME  Catalog only this bucket
"""

import os, sys, json, argparse, datetime
from pathlib import Path
import boto3
from botocore.config import Config

def get_client():
    return boto3.client("s3",
        endpoint_url=os.environ["jurisdiction"],
        aws_access_key_id=os.environ["Access_Key_ID"],
        aws_secret_access_key=os.environ["Secret_Access_Key"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )

def human_size(size):
    for unit in ["B","KB","MB","GB","TB"]:
        if size < 1024: return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"

def list_objects(client, bucket, prefix=""):
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    while True:
        resp = client.list_objects_v2(**kwargs)
        yield from resp.get("Contents", [])
        if not resp.get("IsTruncated"): break
        kwargs["ContinuationToken"] = resp["NextContinuationToken"]

def ext(key):
    return Path(key).suffix.lower() or "(no ext)"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/r2-catalog.json")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--text", action="store_true")
    parser.add_argument("--bucket", default=None)
    args = parser.parse_args()

    client = get_client()
    now = datetime.datetime.utcnow().isoformat() + "Z"

    # List buckets
    all_buckets = client.list_buckets().get("Buckets", [])
    if args.bucket:
        all_buckets = [b for b in all_buckets if b["Name"] == args.bucket]

    catalog = {"generated": now, "buckets": {}, "summary": {}}
    grand_total_size = 0
    grand_total_count = 0

    for b in all_buckets:
        name = b["Name"]
        created = b["CreationDate"].isoformat()
        print(f"  Cataloging {name}...", end=" ", flush=True)

        objects = []
        total_size = 0
        ext_counts = {}

        for obj in list_objects(client, name):
            key = obj["Key"]
            size = obj.get("Size", 0)
            modified = obj["LastModified"].isoformat()
            e = ext(key)

            objects.append({"key": key, "size": size, "modified": modified})
            total_size += size
            ext_counts[e] = ext_counts.get(e, 0) + 1

        grand_total_size += total_size
        grand_total_count += len(objects)

        catalog["buckets"][name] = {
            "created": created,
            "object_count": len(objects),
            "total_size": total_size,
            "total_size_human": human_size(total_size),
            "extensions": dict(sorted(ext_counts.items(), key=lambda x: -x[1])),
            "objects": sorted(objects, key=lambda o: o["key"]),
        }
        print(f"{len(objects)} objects, {human_size(total_size)}")

    catalog["summary"] = {
        "bucket_count": len(all_buckets),
        "total_objects": grand_total_count,
        "total_size": grand_total_size,
        "total_size_human": human_size(grand_total_size),
    }

    # Write JSON catalog
    with open(args.out, "w") as f:
        json.dump(catalog, f, indent=2)
    print(f"\nCatalog written: {args.out}")
    print(f"Summary: {len(all_buckets)} buckets, {grand_total_count:,} objects, {human_size(grand_total_size)}")

    # Write text summary
    text_out = args.out.replace(".json", ".txt")
    if args.text or args.upload:
        with open(text_out, "w") as f:
            f.write(f"R2 STORAGE CATALOG\n")
            f.write(f"Generated: {now}\n")
            f.write(f"{'='*70}\n\n")
            f.write(f"SUMMARY\n")
            f.write(f"  Buckets : {len(all_buckets)}\n")
            f.write(f"  Objects : {grand_total_count:,}\n")
            f.write(f"  Size    : {human_size(grand_total_size)}\n\n")
            f.write(f"{'='*70}\n\n")
            f.write(f"BUCKETS\n\n")

            for name, info in sorted(catalog["buckets"].items()):
                f.write(f"  r2://{name}/\n")
                f.write(f"    Created : {info['created'][:10]}\n")
                f.write(f"    Objects : {info['object_count']:,}\n")
                f.write(f"    Size    : {info['total_size_human']}\n")
                if info["extensions"]:
                    top = list(info["extensions"].items())[:5]
                    f.write(f"    Types   : {', '.join(f'{e}({n})' for e,n in top)}\n")
                f.write(f"\n")

            f.write(f"{'='*70}\n\n")
            f.write(f"ALL OBJECTS\n\n")
            for name, info in sorted(catalog["buckets"].items()):
                f.write(f"\n--- r2://{name}/ ({info['object_count']} objects, {info['total_size_human']}) ---\n")
                for obj in info["objects"]:
                    f.write(f"  {obj['modified'][:19]}  {human_size(obj['size']):>10}  {obj['key']}\n")

        print(f"Text summary written: {text_out}")

    # Upload to R2
    if args.upload:
        import boto3
        date_tag = now[:10]
        for local, key in [
            (args.out, f"catalog/r2-catalog-{date_tag}.json"),
            (args.out, "catalog/r2-catalog-latest.json"),
            (text_out, f"catalog/r2-catalog-{date_tag}.txt"),
            (text_out, "catalog/r2-catalog-latest.txt"),
        ]:
            client.upload_file(local, "claude", key)
            print(f"Uploaded: r2://claude/{key}")

if __name__ == "__main__":
    main()
