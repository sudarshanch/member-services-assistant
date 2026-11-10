"""Command line for a multi-turn rep conversation."""

from __future__ import annotations

import argparse

from member_services.config import load_settings
from member_services.service import Assistant


def main():
    parser = argparse.ArgumentParser(description="Member-services assistant")
    parser.add_argument("--pack", help="Task pack root. Defaults to PACK_DIR or auto-discovery.")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--member", required=True)
    parser.add_argument("--message", action="append", default=[], help="Send this turn and exit. Repeat for a scripted call.")
    args = parser.parse_args()
    settings = load_settings(args.pack)
    assistant = Assistant(settings)
    session = assistant.open(args.tenant, args.member)
    print(f"conversation {session.conversation_id}  tenant {args.tenant}  member {args.member}  as of {assistant.as_of}")
    if args.message:
        for text in args.message:
            result = assistant.turn(session.conversation_id, text)
            print(result["reply"])
            print(f"[steps {result['steps']}  cost ${result['cost_usd']:.4f}]")
        return
    while True:
        try:
            text = input("rep> ").strip()
        except EOFError:
            return
        if not text or text in {":q", "quit", "exit"}:
            return
        result = assistant.turn(session.conversation_id, text)
        print(result["reply"])


if __name__ == "__main__":
    main()
