import argparse
import json
from pathlib import Path
from .storage import TaskStore
from .service import RFQService


def main():
    parser = argparse.ArgumentParser(description="RFQ scaffold: offline demo and optional Genesis agent")
    parser.add_argument("command", choices=["demo", "agent"])
    parser.add_argument("--input", type=Path, required=True, help="Normalized RFQ JSON; raw documents are not implemented yet")
    parser.add_argument("--output", type=Path, default=Path("runtime"))
    args = parser.parse_args()
    service = RFQService(TaskStore(args.output))
    try:
        task = service.create_task(json.loads(args.input.read_text(encoding="utf-8")))
        task_id = task["task_id"]
        if args.command == "demo":
            service.analyze_rfq(task_id)
            service.build_quote_options(task_id, 1)
            result = service.export_quote_draft(task_id, 1)
        else:
            from .agent import create_agent
            agent = create_agent(service)
            response = agent.invoke({"messages": [{"role": "user", "content":
                f"分析任务 {task_id}，生成报价候选及草案。不要修改原始数量。"}]},
                config={"recursion_limit": 20})
            result = {"task": service.get_task_result(task_id),
                      "answer": response["messages"][-1].content}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError, ImportError) as exc:
        parser.exit(2, f"RFQ error: {exc}\n")


if __name__ == "__main__":
    main()
