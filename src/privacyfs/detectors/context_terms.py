"""Domain and quasi-identifier terms for combination-risk analysis.

These signals are not added to the ordinary filename masking pipeline. They
provide release-level context to the M2 audit engine, where several individually
weak signals may form a strong inference.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .keywords import Finding

_TERMS: dict[str, tuple[str, ...]] = {
    "MEDICAL": (
        "住院", "出院", "手术", "化疗", "放疗", "病历", "诊断", "处方",
        "复查", "病理", "检验报告", "肿瘤", "癌症", "精神科", "心理咨询",
        "医保", "产检", "HIV",
    ),
    "LEGAL": (
        "起诉", "判决", "被告", "原告", "仲裁", "取保候审", "案底", "拘留",
        "看守所", "服刑", "缓刑", "律师函", "证人", "赔偿", "离婚",
    ),
    "FINANCIAL": (
        "贷款", "房贷", "车贷", "信用卡", "征信", "逾期", "催收", "工资",
        "薪资", "银行流水", "个税", "破产", "抵押",
    ),
    "EDUCATION": (
        "大学", "学院", "中学", "小学", "学校", "学生", "毕业", "答辩",
        "奖学金", "成绩单", "学籍", "教务处", "导师", "实验室",
    ),
    "MAJOR": (
        "计算机", "软件工程", "人工智能", "电子信息", "金融学", "法学",
        "医学", "临床医学", "工商管理", "会计学", "外国语言文学",
    ),
    "POLITICAL": (
        "党员", "入党", "党课", "思想汇报", "政审", "党费", "团组织",
    ),
    "FAMILY": (
        "父亲", "母亲", "父母", "配偶", "妻子", "丈夫", "子女", "家属",
        "亲子鉴定", "抚养权", "赡养", "领养",
    ),
    "AWARD": ("一等奖", "二等奖", "三等奖", "奖学金", "优秀学生", "获奖"),
}

@lru_cache(maxsize=1)
def _patterns():
    from ..config import expand_terms
    return {
        category: re.compile("|".join(re.escape(term) for term in sorted(expand_terms(list(terms)), key=len, reverse=True)), re.IGNORECASE)
        for category, terms in _TERMS.items()
    }


class ContextTermDetector:
    """Detect domain-sensitive events and weak quasi-identifiers."""

    def find(self, text: str) -> list[Finding]:
        findings: list[Finding] = []
        for category, pattern in _patterns().items():
            findings.extend(Finding(category, match.group(0)) for match in pattern.finditer(text))
        return findings
