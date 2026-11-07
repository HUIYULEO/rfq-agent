"""Input-understanding provenance; never changes RFQ or pricing decisions."""

FALLBACK_REASONS = {
    "not_configured": "模型未配置，使用规则解析；本次未完成模型理解。",
    "timeout": "模型请求超时，已降级为规则解析；本次未完成模型理解。",
    "network_error": "模型接口连接失败，已降级为规则解析；本次未完成模型理解。",
    "http_error": "模型接口返回错误，已降级为规则解析；本次未完成模型理解。",
    "invalid_response": "模型返回格式无效，已降级为规则解析；本次未完成模型理解。",
    "invalid_json": "模型未返回可解析的 JSON，已降级为规则解析；本次未完成模型理解。",
    "validation_failed": "模型理解结果不可用，已降级为规则解析；本次未完成模型理解。",
    "model_error": "模型理解失败，已降级为规则解析；本次未完成模型理解。",
    "legacy_fallback": "已有记录表明使用了规则解析，具体降级原因未结构化记录。",
}


def fallback_report(code: str, model: str = "") -> dict:
    code = code if code in FALLBACK_REASONS else "model_error"
    return {"source": "deterministic", "mode": "deterministic", "model": model,
            "model_ran": False, "degraded": True, "fallback_code": code,
            "reason": FALLBACK_REASONS[code]}


def input_status(report: dict | None) -> dict:
    """model_ran means usable understanding completed, not merely an HTTP attempt.

    A legacy recorded source is usable evidence; no record means unknown. Raw
    exception strings from legacy reports are not republished in the status.
    """
    report = report or {}
    source = report.get("source")
    if source == "deterministic" or report.get("degraded") is True:
        code = report.get("fallback_code") or "legacy_fallback"
        code = code if code in FALLBACK_REASONS else "model_error"
        return {"status": "degraded", "label": "已降级：仅规则解析",
                "model_ran": False, "degraded": True, "fallback_code": code,
                "reason": FALLBACK_REASONS[code]}
    if source in {"agent", "agent+deterministic"} and report.get("model_ran") is not False:
        extracted = source == "agent"
        return {"status": "completed", "model_ran": True, "degraded": False,
                "fallback_code": "",
                "label": "模型抽取完成" if extracted else "规则解析 + 模型审查完成",
                "reason": ("模型抽取结果已通过现有字段校验；报价数字由规则引擎计算。" if extracted
                           else "模型审查了提供的内容与解析摘要；行数据和报价数字由规则引擎处理。")}
    return {"status": "unknown", "label": "模型理解状态未记录", "model_ran": None,
            "degraded": None, "fallback_code": "",
            "reason": "此任务没有完整的模型理解记录，无法确认是否完成模型审查。"}
