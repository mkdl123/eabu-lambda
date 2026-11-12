#!/usr/bin/env python3
"""Delete an eabu account and its data, the same way the app's Delete Account does.

Use it for deletion requests that came in by email, or ones left in the eabu-delete table
(by the old Delete Account button, or a failed run). It permanently deletes the account.

    python3 scripts/delete_user.py --pending          # list requests waiting in eabu-delete
    python3 scripts/delete_user.py --pending --yes    # process all of them
    python3 scripts/delete_user.py <userId> --yes     # delete one account

Runs with your local AWS credentials.
"""
import argparse
import importlib.util
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "eabu-dish", "python"))
spec = importlib.util.spec_from_file_location(
    "eabu_delete_user_info", os.path.join(ROOT, "eabu-dish", "eabu-delete-user-info", "app.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("user_id", nargs="?", help="Cognito username (the app's userId)")
    parser.add_argument("--pending", action="store_true", help="use the requests waiting in eabu-delete")
    parser.add_argument("--yes", action="store_true", help="actually delete (otherwise only list)")
    args = parser.parse_args()

    if args.pending:
        users = [str(r["userId"]) for r in app._scan_all(app.delete_requests)]
    elif args.user_id:
        users = [args.user_id]
    else:
        parser.error("give a userId or --pending")

    print(f"{len(users)} account(s) to delete")
    if not args.yes:
        print("Nothing deleted; add --yes to delete.")
        return
    for user_id in users:
        counts = app.delete_user(user_id)
        print(f"deleted one account: {sum(counts.values())} rows removed")


if __name__ == "__main__":
    main()
