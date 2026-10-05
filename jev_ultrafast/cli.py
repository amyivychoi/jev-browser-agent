"""Run one browser task from the local command line."""

import argparse
import json
import sys

def main():
    parser = argparse.ArgumentParser(description="Run a bounded browser-use task against your Chrome profile.")
    parser.add_argument("--url", required=True, help="Starting page URL")
    parser.add_argument("--goal", required=True, help="Natural-language browser task")
    parser.add_argument("--expect-text", help="Optional visible page text required to report verified completion")
    args = parser.parse_args()
    if len(args.goal) > 2000:
        parser.error("--goal must be 2,000 characters or fewer")

    from .agent import Agent

    agent = None
    verified = False
    try:
        agent = Agent(args.url, args.goal)
        print(json.dumps({"event": "started", "url": agent.state["page"]["url"], "title": agent.state["page"]["title"]}))
        for state in agent.run():
            event = {
                "event": "step",
                "status": state["status"],
                "url": state["page"]["url"],
                "elapsed_ms": state["elapsed_ms"],
                "last_action": state["history"][-1]["action"] if state["history"] else None,
                "decision_model": state["decisions"][-1]["model"] if state["decisions"] else None,
            }
            if state["status"] == "done":
                page_text = state["page"]["text"]
                if args.expect_text:
                    visible_page = f"{state['page'].get('title', '')}\n{page_text}"
                    event["verified"] = args.expect_text.casefold() in visible_page.casefold()
                    verified = event["verified"]
                    event["verification_text"] = args.expect_text
                    if not event["verified"]:
                        event["status"] = "done_unverified"
                else:
                    event["status"] = "done_unverified"
                    event["verification"] = "No --expect-text supplied; inspect the page to confirm the outcome."
            print(json.dumps(event, ensure_ascii=False))
        return 0 if verified else 2
    except (RuntimeError, ValueError, TimeoutError) as error:
        print(json.dumps({"event": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if agent is not None:
            agent.close()


if __name__ == "__main__":
    raise SystemExit(main())
