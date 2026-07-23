from __future__ import annotations


INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "投诉": ("投诉", "举报", "差评", "不满意", "欺骗", "太差", "维权"),
    "售后问题": ("退款", "退货", "换货", "保修", "维修", "坏了", "物流", "发票", "售后"),
    "产品咨询": ("产品", "价格", "功能", "型号", "支持", "购买", "套餐", "区别", "怎么用"),
    "闲聊": ("你好", "谢谢", "在吗", "再见", "早上好", "晚安", "你是谁"),
}


def classify_intent(question: str) -> str:
    scores = {
        intent: sum(question.lower().count(keyword.lower()) for keyword in keywords)
        for intent, keywords in INTENT_KEYWORDS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "其他"


def suggestions_for(intent: str, sources: list[str]) -> list[str]:
    by_intent = {
        "产品咨询": ["这个产品适合哪些场景？", "不同方案之间有什么区别？", "如何开始使用？"],
        "售后问题": ["办理时需要准备什么材料？", "处理通常需要多久？", "如何查询处理进度？"],
        "投诉": ["如何提交正式投诉？", "投诉后多久会有回复？", "我需要提供哪些凭证？"],
        "闲聊": ["你可以帮我查询哪些问题？", "如何使用知识库问答？"],
    }
    result = by_intent.get(intent, ["能否说明具体适用条件？", "还有哪些注意事项？"])
    if sources:
        result = result[:2] + [f"请概括《{sources[0]}》的重点"]
    return result[:3]
