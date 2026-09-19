"""Chinese person-name detection via jieba POS tagging (lazy-loaded)."""

from __future__ import annotations

import re
import unicodedata

from .keywords import Finding

_CJK = re.compile(r"[一-鿿]")

# Common surnames; NER hits not starting with one are treated as noise
# (jieba's nr tag fires on words like "法律文书" surprisingly often).
_SINGLE_SURNAMES = frozenset(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭"
    "曾肖田董潘袁蔡蒋余于杜叶程苏魏吕丁任卢沈钟姜崔谭陆范汪廖石金"
    "韦贾夏付方白邹孟熊秦邱江尹薛闫段雷侯龙陶贺顾毛郝龚邵万钱严覃"
    "武戴莫孔向汤"
    # traditional forms
    "張劉陳楊黃趙吳孫馬朱胡郭何羅鄭梁謝許韓鄧曹鄭曾蕭田董潘袁蔡蔣"
    "餘於杜葉程蘇魏呂丁盧沈鍾薑崔譚陸範汪廖金韋賈鄒熊秦邱江尹薛"
    "閆段雷侯龍陶賀顧毛郝龔邵萬錢嚴覃武戴莫孔嚮湯孫許馮鄧龐費"
)
_COMPOUND_SURNAMES = frozenset(
    ["欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟",
     "闻人", "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政",
     "濮阳", "公冶", "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙",
     "宇文", "司徒", "鲜于", "司空", "令狐", "轩辕",
     # traditional forms
     "歐陽", "端木", "上官", "司馬", "東方", "獨孤", "南宮", "聞人", "夏侯",
     "諸葛", "尉遲", "赫連", "澹臺", "皇甫", "濮陽", "申屠", "公孫", "慕容",
     "鍾離", "長孫", "宇文", "司徒", "鮮於", "司空", "令狐", "軒轅"]
)


def _looks_like_name(token: str) -> bool:
    return token[0] in _SINGLE_SURNAMES or token[:2] in _COMPOUND_SURNAMES

_posseg = None


def _load():
    global _posseg
    if _posseg is None:
        import jieba.posseg as posseg

        _posseg = posseg
    return _posseg


class NameNER:
    """Flags jieba tokens tagged nr (person name). Skips names with no CJK.

    Runs a second pass on the NFKC + simplified form (jieba's dictionary is
    simplified Chinese); surfaces found only there are mapped back to the
    original text via s2t so replacement can still locate them.
    """

    def find(self, name: str) -> list[Finding]:
        if not _CJK.search(name):
            return []
        from ..config import _to_traditional, to_simplified

        posseg = _load()
        variants = [name]
        norm = to_simplified(unicodedata.normalize("NFKC", name))
        if norm != name:
            variants.append(norm)

        findings: list[Finding] = []
        seen: set[str] = set()
        for text in variants:
            for word, flag in posseg.cut(text):
                if not (flag.startswith("nr") and len(word) >= 2 and _looks_like_name(word)):
                    continue
                for surface in (word, _to_traditional(word)):
                    if surface in name and surface not in seen:
                        seen.add(surface)
                        findings.append(Finding("PERSON", surface))
                        break
        return findings


# Given-name characters that mark a non-name ("王的盛宴", "张了了").
_NAME_STOP_CHARS = "的了之和与及或是我你他她它们在有就把都被让从往到着过呢吗吧啊嘛么"

# Common words that start with a surname character but are not names.
_FALSE_POSITIVES = frozenset(
    "方法 方式 方面 方向 方便 方案 马上 马虎 马路 曾经 程序 程度 方程式 "
    "范围 范畴 范例 范式 高中 高考 高速 高清 高度 高级 高潮 高压 高温 高铁 "
    "金额 金融 金属 基金 金钱 金色 钱包 白色 白菜 白天 白酒 江苏 江湖 江西 "
    "苏州 苏联 苏醒 夏天 夏季 雷达 雷锋 雷雨 雷电 石头 石墨 石油 于是 "
    "毛巾 毛病 任何 任务 任性 任意 沈阳 时钟 钟表 万一 万能 严格 严肃 "
    "严重 严谨 武汉 武器 武侠 莫非 莫名 向往 向前 龙王 龙头 孔子 孔雀 "
    "孟子 姜汤 卢森堡 董事 董事长 庞大 费用 薛定谔".split()
)


class SurnameHeuristic:
    """Boundary-anchored surname detector, NER 之外的兜底.

    A candidate is surname + 1-2 given-name chars, not adjacent to other CJK
    on either side (so "张三-论文" matches, "王者荣耀攻略" does not). Cheaper
    than NER and catches names jieba misses; the false-positive list keeps
    common words ("方法", "任务", "高清") out.
    """

    def __init__(self):
        compound = "|".join(sorted(_COMPOUND_SURNAMES, key=len, reverse=True))
        single = "".join(sorted(_SINGLE_SURNAMES))
        body = rf"(?:{compound}[一-鿿]{{1,2}}|[{single}][一-鿿]{{1,2}})"
        self._name = re.compile(rf"(?<![一-鿿])({body})(?![一-鿿])")

    def find(self, name: str) -> list[Finding]:
        if not _CJK.search(name):
            return []
        out: list[Finding] = []
        for m in self._name.finditer(name):
            token = m.group(1)
            if token in _FALSE_POSITIVES:
                continue
            if any(c in _NAME_STOP_CHARS for c in token[1:]):
                continue
            out.append(Finding("PERSON", token))
        return out
