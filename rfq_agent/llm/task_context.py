"""Deterministic, query-sized task context for the conversational assistant."""
from collections import Counter
import json

from rfq_agent.domain import quality, terms
from rfq_agent.domain.pricing import summarize

MAX_RESULT_CHARS = 12000


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def issue_rows(task):
    """One row per review/finding, never a second copy from interruptions."""
    rows = []
    for item in task.get("review_items", []):
        if item["status"] != "open":
            continue
        question = item["question"]
        rows.append({"id": item["id"], "source": "review", "kinds": item["kinds"],
                     "severity": item.get("severity", "review"), "target": item["target"],
                     "question": question[:240], "question_shortened": len(question) > 240,
                     "detail_tool": "get_review_item"})
    for item in quality.inspect(task["rfq"]):
        rows.append({"id": "quality:" + item["kind"], "source": "quality",
                     "kinds": [item["kind"]], "severity": item["severity"],
                     "title": item["title"], "question": item["detail"],
                     "affected_count": item.get("count"), "target": {}})
    for index, detail in enumerate(task.get("evaluation_issues", [])):
        rows.append({"id": f"evaluation:{index}", "source": "evaluation",
                     "kinds": ["evaluation_incomplete"], "severity": "blocking",
                     "question": detail, "target": {}})
    return rows


def overview(task):
    rows = issue_rows(task)
    reviews = [r for r in rows if r["source"] == "review"]
    groups = []
    for kind in sorted({kind for r in reviews for kind in r["kinds"]}):
        matching = [r for r in reviews if kind in r["kinds"]]
        line_numbers = sorted({r["target"]["line_number"] for r in matching
                               if r["target"].get("line_number") is not None})
        groups.append({"kind": kind, "review_count": len(matching),
                       "affected_line_count": len(line_numbers),
                       "example_lines": line_numbers[:3],
                       "example_item_ids": [r["id"] for r in matching[:2]]})
    pending = [i for i in task.get("interruptions", []) if i.get("status") == "pending"]
    return {
        "task_id": task["task_id"], "revision": task["revision"],
        "status": task["status"], "process_status": task.get("process_status", task["status"]),
        "evaluation_status": task.get("evaluation_status", "pending"),
        "commercial_disposition": task.get("commercial_disposition", "PENDING"),
        "customer": task["rfq"].get("customer"), "rfq_number": task["rfq"].get("rfq_number"),
        "summary": summarize(task["results"]),
        "open_review_count": len(reviews), "review_groups": groups,
        "counting_note": "review_count 按复核项计数；affected_line_count 按行号去重。一个复核项可涉及多类问题，各类数量不能相加作为总数。quality 的 info 是中性事实，不是报价阻塞。",
        "quality": [{k: r[k] for k in ("id", "kinds", "severity", "title", "question", "affected_count")}
                    for r in rows if r["source"] == "quality"],
        "terms": [{"text": i["text"], "status": i["label"], "detail": i["detail"]}
                  for i in terms.check(task["rfq"], task["results"])],
        "evaluation_issues": task.get("evaluation_issues", []),
        "pending_gate_counts": dict(Counter(i.get("gate", i.get("type", "unknown")) for i in pending)),
        "next_action": ("需要用户逐项确认；用 list_task_issues 查列表，get_review_item 查证据和选项。"
                        if reviews or pending else "没有待确认门禁；依据当前评估状态回答，不自动导出或激活报价。"),
        "detail_tools": ["list_task_issues", "explain_line", "get_review_item"],
    }


def page_rows(rows, cursor=0, limit=10, **metadata):
    """Page by whole records and publish the actual next cursor, never cut JSON."""
    cursor = int(cursor or 0)
    if cursor < 0 or cursor > len(rows):
        raise ValueError("cursor 超出范围，请从 0 开始查询")
    limit = max(1, min(20, int(limit or 10)))
    selected = rows[cursor:cursor + limit]
    def payload():
        end = cursor + len(selected)
        return {**metadata, "total": len(rows), "cursor": cursor, "items": selected,
                "next_cursor": end if end < len(rows) else None}
    while len(selected) > 1 and len(encode(payload())) > MAX_RESULT_CHARS:
        selected.pop()
    return payload()


def review_detail(task, item_id, cursor=0, limit=10):
    item = next((i for i in task.get("review_items", []) if i["id"] == item_id), None)
    if item is None:
        raise ValueError(f"没有复核项 {item_id}")
    result = {k: item.get(k) for k in ("id", "scope", "kinds", "target", "severity",
                                      "question", "options", "status", "answer", "locator")}
    # Gate metadata is useful; its duplicated question/evidence/options are not.
    result["gates"] = [{k: i.get(k) for k in ("id", "gate", "status", "revision", "proposed_action")}
                       for i in task.get("interruptions", []) if i.get("review_item_id") == item_id
                       and i.get("status") == "pending"]
    result.update(task_id=task["task_id"], revision=task["revision"])
    result["evidence"] = page_rows(item.get("evidence", []), cursor, limit)
    return result


def tool_message(result, name):
    body = encode(result)
    if len(body) <= MAX_RESULT_CHARS:
        return body
    # A single huge source record is an explicit failure, never partial evidence.
    return encode({"error": "tool_result_too_large", "tool": name,
                   "task_id": result.get("task_id"), "revision": result.get("revision"),
                   "message": "结果超出单次上下文预算，未提供内容。请缩小查询范围或分页；若单条仍过长，说明缺口并请用户查看原件，不重复同一查询。"})
