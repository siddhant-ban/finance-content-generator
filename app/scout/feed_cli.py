"""Curator subcommands for the local Scout CLI."""
import argparse
from contextlib import closing
import json
from pathlib import Path

from app.scout.registry import FeedRegistry, FeedSettings, RIGHTS, SOURCE_CLASSES
from app.storage.db import migrate, open_database


def add_feed_commands(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", type=Path, required=True, help="Scout run directory containing scout.sqlite3")
    commands = parser.add_subparsers(dest="feed_command", required=True)
    commands.add_parser("seed")
    commands.add_parser("list").add_argument("--history", action="store_true")
    commands.add_parser("snapshot").add_argument("--hash", dest="digest")
    for name in ("add", "revise", "disable"):
        command = commands.add_parser(name)
        command.add_argument("feed_id")
        command.add_argument("--actor", required=True)
        command.add_argument("--reason", required=True)
        if name != "disable":
            fields = {
                "endpoint": {}, "publisher": {}, "source-class": {"choices": SOURCE_CLASSES},
                "rights": {"choices": RIGHTS}, "usage-notes": {},
            }
            for field, options in fields.items():
                command.add_argument(f"--{field}", required=name == "add", **options)
        if name == "revise":
            command.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)


def manage_feeds(args: argparse.Namespace) -> int:
    args.output.mkdir(parents=True, exist_ok=True)
    with closing(open_database(args.output / "scout.sqlite3")) as conn:
        migrate(conn)
        registry = FeedRegistry(conn)
        if args.feed_command == "seed":
            payload = [row.model_dump(mode="json") for row in registry.seed()]
        elif args.feed_command == "list":
            payload = [row.model_dump(mode="json") for row in registry.list(history=args.history)]
        elif args.feed_command == "snapshot":
            snapshot = registry.snapshot(args.digest)
            payload = snapshot.model_dump(mode="json") | {"sha256": snapshot.record_hash()}
        else:
            fields = {name: getattr(args, name) for name in FeedSettings.model_fields
                      if getattr(args, name, None) is not None}
            if args.feed_command == "add":
                row = registry.add(args.feed_id, FeedSettings.model_validate(fields),
                                   actor=args.actor, reason=args.reason)
            else:
                changes = {"enabled": False} if args.feed_command == "disable" else fields
                row = registry.revise(args.feed_id, changes=changes, actor=args.actor, reason=args.reason)
            payload = row.model_dump(mode="json")
        print(json.dumps(payload, indent=2))
    return 0
