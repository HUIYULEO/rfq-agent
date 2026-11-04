import argparse
import json
from pathlib import Path

from rfq_agent.infrastructure.storage import TaskStore
from rfq_agent.application.service import RFQService
from rfq_agent.domain.pricing import summarize


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _run_case(args, service: RFQService):
    from rfq_agent.application.cases import load_case, template_for

    root = Path.cwd()
    rfq, provider = load_case(root, args.case)
    service.provider = provider
    service.template = template_for(root)
    task = service.create_task(rfq.to_dict(), autonomy=args.autonomy)
    task_id, revision = task["task_id"], task["revision"]
    service.analyze_rfq(task_id)
    state = service.build_quote_options(task_id, revision)

    if args.resolve:
        for pair in args.resolve:
            item_id, _, option = pair.partition("=")
            if not option:
                raise ValueError(f"--resolve 需要 ITEM=OPTION 形式，收到 {pair!r}")
            state = service.resolve_review(task_id, state["revision"], item_id, option)

    output = {
        "task_id": task_id,
        "status": state["status"],
        "autonomy": state["autonomy"],
        "revision": state["revision"],
        "summary": summarize(state["results"]),
        "review_items": [
            {"id": item["id"], "scope": item["scope"], "kinds": item["kinds"],
             "target": item["target"], "status": item["status"],
             "options": [option["id"] for option in item["options"]]}
            for item in state["review_items"]
        ],
    }
    if args.export:
        output["export"] = service.export_quote_draft(task_id, state["revision"])
    _print(output)


def main():
    parser = argparse.ArgumentParser(description="RFQ agent: offline demo, official case, optional Genesis agent")
    parser.add_argument("command", choices=["demo", "case", "agent"])
    parser.add_argument("--input", type=Path, help="Normalized RFQ JSON (demo/agent)")
    parser.add_argument("--case", default="B", help="Official case key, e.g. B")
    parser.add_argument("--autonomy", default="interactive", choices=["interactive", "auto_draft"])
    parser.add_argument("--resolve", action="append", metavar="ITEM=OPTION",
                        help="Answer a review item, e.g. --resolve RI-0001=quote_with_moq")
    parser.add_argument("--export", action="store_true", help="Render the DOCX draft")
    parser.add_argument("--output", type=Path, default=Path("runtime"))
    args = parser.parse_args()

    service = RFQService(TaskStore(args.output))
    try:
        if args.command == "case":
            _run_case(args, service)
            return
        if not args.input:
            parser.error("--input is required for demo/agent")
        task = service.create_task(json.loads(args.input.read_text(encoding="utf-8")))
        task_id = task["task_id"]
        if args.command == "demo":
            service.analyze_rfq(task_id)
            service.build_quote_options(task_id, 1)
            result = service.export_quote_draft(task_id, 1)
        else:
            from rfq_agent.llm.agent import create_agent
            agent = create_agent(service)
            response = agent.invoke({"messages": [{"role": "user", "content":
                f"分析任务 {task_id}，生成报价候选及草案。不要修改原始数量。"}]},
                config={"recursion_limit": 20})
            result = {"task": service.get_task_result(task_id),
                      "answer": response["messages"][-1].content}
        _print(result)
    except (ValueError, KeyError, OSError, ImportError) as exc:
        parser.exit(2, f"RFQ error: {exc}\n")


if __name__ == "__main__":
    main()