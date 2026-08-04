"""CLI wrapper for the easy Claude Projects API.

Registered as the ``ctxsync-easy`` console script. Sub-commands:

  ctxsync-easy orgs
  ctxsync-easy projects [--all] [--json]
  ctxsync-easy docs <project>
  ctxsync-easy upload <project> <file_name> [<local_path>|-]
  ctxsync-easy create <name> [--desc TEXT]
  ctxsync-easy archive <project>

``<project>`` accepts a project uuid or a case-insensitive name. Reading
content from stdin uses ``-`` as the local path (e.g. ``echo hi | ...
ctxsync-easy upload Bookmarked hi.md -``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .easy import claude_projects


def _emit(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(obj)


def _cmd_orgs(args, cp) -> int:
    orgs = cp.organizations()
    if args.json:
        _emit(orgs, True)
    else:
        for org in orgs:
            print(f"{org['id']}  {org['name']}")
    return 0


def _cmd_projects(args, cp) -> int:
    projects = cp.list_projects(include_archived=args.all)
    if args.json:
        _emit(projects, True)
    else:
        for project in projects:
            marker = " (archived)" if project.get("archived_at") else ""
            print(f"{project['id']}  {project['name']}{marker}")
    return 0


def _cmd_docs(args, cp) -> int:
    docs = cp.docs(args.project)
    if args.json:
        _emit(docs, True)
    else:
        for doc in docs:
            print(f"{doc['uuid']}  {doc['file_name']}")
    return 0


def _cmd_upload(args, cp) -> int:
    if args.source == "-":
        content = sys.stdin.read()
    else:
        content = Path(args.source).read_text(encoding="utf-8")
    result = cp.upload(args.project, args.file_name, content)
    _emit(result, args.json)
    return 0


def _cmd_create(args, cp) -> int:
    result = cp.create_project(args.name, args.desc or "")
    _emit(result, args.json)
    return 0


def _cmd_archive(args, cp) -> int:
    result = cp.archive_project(args.project)
    _emit(result, args.json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctxsync-easy")
    parser.add_argument(
        "--org", help="Override active org uuid (else ORG_ID env / auto-pick)"
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("orgs", help="List usable organizations")

    projects_parser = sub.add_parser("projects", help="List projects")
    projects_parser.add_argument("--all", action="store_true", help="Include archived")

    docs_parser = sub.add_parser("docs", help="List documents in a project")
    docs_parser.add_argument("project", help="Project uuid or name")

    upload_parser = sub.add_parser("upload", help="Upload a file to a project")
    upload_parser.add_argument("project", help="Project uuid or name")
    upload_parser.add_argument("file_name", help="File name in the project")
    upload_parser.add_argument(
        "source", nargs="?", default="-", help="Local path or '-' for stdin (default)"
    )

    create_parser = sub.add_parser("create", help="Create a new project")
    create_parser.add_argument("name")
    create_parser.add_argument("--desc", default="")

    archive_parser = sub.add_parser("archive", help="Archive a project")
    archive_parser.add_argument("project", help="Project uuid or name")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cp = claude_projects(org_id=args.org)
    handler = {
        "orgs": _cmd_orgs,
        "projects": _cmd_projects,
        "docs": _cmd_docs,
        "upload": _cmd_upload,
        "create": _cmd_create,
        "archive": _cmd_archive,
    }[args.cmd]
    return handler(args, cp)


if __name__ == "__main__":
    sys.exit(main())
