"""Rule loading and defaults.

A rules file is YAML with any of these keys (all optional, merged over defaults):

    keywords:           [str]  sensitive substrings (case-insensitive for ASCII)
    regexes:            [str]  raw regex patterns
    literal_names:      [str]  exact personal names / org names to always mask
    org_suffixes:       [str]  extra organization suffixes (公司/集团/...)
    professions:        [str]  extra professions / job titles
    hidden_keywords:    [str]  a dir whose name contains one of these is hidden whole
    excludes:           [str]  fnmatch-style names to skip entirely (not reported)
    use_ner:            bool   jieba person-name detection (default true)
    detect_orgs:        bool   organization detection (default true)
    detect_professions: bool   profession / job-title masking (default true)

The rules file itself may contain your real name, employer, etc. Keep it
OUTSIDE any directory you scan.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import yaml

_FULLWIDTH = {i: i + 0xFEE0 for i in range(0x21, 0x7F)}  # printable ASCII -> fullwidth
_CJK = re.compile(r"[一-鿿]")

_s2t = None
_t2s = None


def _to_traditional(text: str) -> str:
    global _s2t
    if _s2t is None:
        from opencc import OpenCC

        _s2t = OpenCC("s2t")
    return _s2t.convert(text)


def to_simplified(text: str) -> str:
    """Traditional -> simplified (for feeding jieba, whose dict is simplified)."""
    global _t2s
    if _t2s is None:
        from opencc import OpenCC

        _t2s = OpenCC("t2s")
    return _t2s.convert(text)


def expand_terms(terms: list[str]) -> list[str]:
    """Generate matching variants of each term: NFKC-normalized, full-width
    ASCII (R18 -> Ｒ１８), and simplified->traditional Chinese (里番 -> 裏番).
    Detectors match the raw filename, so variants must be materialized here.
    """
    out: set[str] = set()
    for term in terms:
        variants = {term, unicodedata.normalize("NFKC", term), term.translate(_FULLWIDTH)}
        if _CJK.search(term):
            variants.add(_to_traditional(term))
            variants.add(to_simplified(term))
        out.update(v for v in variants if v)
    return sorted(out)


PARANOID_REGEXES: list[str] = [
    r"(?<!\d)[1-9]\d{4,10}(?!\d)",      # QQ number (digit-run bounded, not \b)
    r"(?<!\d)\d{16,19}(?!\d)",          # bank card
]

DEFAULT_KEYWORDS: list[str] = [
    # adult
    "R18", "NSFW", "18禁", "成人影片", "成人向", "里番", "本子", "福利姬",
    # personal / legal / medical
    "法律文书", "起诉", "判决书", "病历", "诊断", "体检报告",
    "职称", "论文", "简历", "身份证", "户口本", "银行流水",
]

DEFAULT_REGEXES: list[str] = [
    # Use digit boundaries rather than \b: CJK and ASCII letters are both
    # regex "word" characters, so \b misses values in names like
    # "联系13812345678.txt" and "ID110101199003078823.pdf".
    r"(?<!\d)1[3-9]\d{9}(?!\d)",          # CN mobile
    r"(?<!\d)\d{17}[\dXx](?!\d)",         # CN ID card
    r"[\w.+-]+@[\w-]+\.[\w.]+",          # email
]

# Organization detection = 2-15 CJK chars followed by one of these suffixes.
DEFAULT_ORG_SUFFIXES: list[str] = [
    "公司", "集团", "事务所", "医院", "诊所", "大学", "学院", "中学", "小学",
    "幼儿园", "银行", "证券", "保险", "工作室", "研究院", "研究所", "协会",
    "基金会", "商会", "出版社", "酒店", "宾馆", "餐厅", "酒楼",
]

DEFAULT_PROFESSIONS: list[str] = [
    "律师", "医生", "护士", "教师", "老师", "教授", "会计", "出纳",
    "工程师", "程序员", "设计师", "记者", "编辑", "演员", "歌手", "司机",
    "厨师", "保安", "保洁", "销售", "顾问", "审计", "翻译", "导游", "模特",
    "主播", "快递员", "公务员", "警察", "法官", "检察官", "军人",
    # job titles
    "经理", "总监", "总裁", "董事长", "法人", "CEO", "CTO", "CFO", "COO",
]

# Identity-implying vocabulary, grouped by PIPL sensitive-personal-info
# categories. These are not entities (NER can't find them); they IMPLY an
# identity or status ("奖学金" -> student, "取保候审" -> involved in a case).
# Kept specific on purpose: overly generic words ("毕业", "药") are excluded
# to limit false positives; tune via rules `identity_terms` / `excludes`.
DEFAULT_IDENTITY_TERMS: list[str] = [
    # 学业身份
    "学生", "校园", "奖学金", "助学金", "助学贷款", "毕业生", "毕业论文",
    "毕业设计", "毕业证", "学籍", "学费", "成绩单", "考研", "保研", "高考",
    "志愿填报", "录取通知", "报到证", "宿舍", "辅导员", "教务处", "选课",
    "学分", "绩点", "GPA", "学生证", "校园卡", "四六级", "专升本", "复读",
    "退学", "休学", "处分", "家长会",
    # 求职与职场身份
    "求职", "面试", "offer", "背调", "入职", "离职", "辞职",
    "裁员", "竞业限制", "劳动合同", "工资", "薪资", "年终奖", "绩效",
    "考勤", "社保", "公积金", "个税", "转正", "试用期", "晋升", "工牌",
    "在职证明", "失业", "再就业",
    # 医疗健康（PIPL 敏感类别）
    "挂号", "住院", "出院", "手术", "化疗", "放疗", "处方", "复查",
    "病理报告", "检验报告", "血常规", "核磁", "B超", "心电图", "医保",
    "慢性病", "肿瘤", "癌症", "艾滋", "HIV", "抑郁", "心理咨询", "精神科",
    "产检", "流产", "试管婴儿", "不孕不育",
    # 法律涉案
    "律师函", "被告", "原告", "上诉", "仲裁", "取保候审", "案底",
    "笔录", "证人", "和解协议", "赔偿", "离婚", "抚养权", "遗产",
    "遗嘱", "公证", "拘留", "看守所", "服刑", "缓刑", "假释",
    # 金融财务（PIPL 敏感类别）
    "贷款", "房贷", "车贷", "信用卡", "征信", "逾期", "催收", "欠条",
    "借条", "花呗", "白条", "网贷", "担保", "抵押", "破产",
    # 政治面貌（PIPL 敏感类别）
    "入党", "党员", "党课", "党章", "思想汇报", "民主生活会", "党费",
    "政审", "团组织", "少先队",
    # 宗教信仰（PIPL 敏感类别）
    "寺庙", "教堂", "清真寺", "礼拜", "受洗", "皈依", "法会", "朝觐",
    "佛经", "圣经", "古兰经",
    # 婚恋家庭
    "相亲", "婚介", "婚纱照", "彩礼", "嫁妆", "出轨", "亲子鉴定",
    "领养", "赡养", "家暴",
    # 性取向（PIPL 敏感类别）
    "出柜", "同性恋", "同志",
    # 军警公职
    "部队", "军衔", "转业", "退伍", "服役", "编制", "保密协议",
    # 出入境与移民
    "签证", "绿卡", "护照", "移民", "入籍", "永居", "遣返",
    # English
    "resume", "curriculum vitae", "salary", "mortgage", "divorce",
    "lawsuit", "visa", "greencard", "medical record",
]

DEFAULT_HIDDEN_KEYWORDS: list[str] = [
    "R18", "NSFW", "18禁", "成人影片", "里番", "本子",
]

DEFAULT_EXCLUDES: list[str] = [
    ".git", ".svn", ".hg", "node_modules", "__pycache__",
    "$Recycle.Bin", "System Volume Information", ".DS_Store",
    ".privacyfs-build-*", ".privacyfs-backup-*", ".privacyfs-lock-*",
    ".privacyfs-transaction-*",
]


@dataclass
class Rules:
    keywords: list[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    regexes: list[str] = field(default_factory=lambda: list(DEFAULT_REGEXES))
    literal_names: list[str] = field(default_factory=list)
    literal_orgs: list[str] = field(default_factory=list)
    identity_terms: list[str] = field(default_factory=lambda: list(DEFAULT_IDENTITY_TERMS))
    org_suffixes: list[str] = field(default_factory=lambda: list(DEFAULT_ORG_SUFFIXES))
    professions: list[str] = field(default_factory=lambda: list(DEFAULT_PROFESSIONS))
    hidden_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_HIDDEN_KEYWORDS))
    excludes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    use_ner: bool = True
    ner_engine: str = "auto"   # auto | hanlp | jieba | off
    detect_orgs: bool = True
    detect_professions: bool = True
    detect_en_names: bool = True
    detect_pinyin: bool = True
    detect_identity: bool = True
    pinyin_allowlist: list[str] = field(default_factory=list)
    paranoid: bool = False
    use_llm: bool = False
    llm_model: str = "privacyfs-minicpm"
    llm_url: str = "http://127.0.0.1:11434"
    llm_max: int = 500          # max unflagged components sent to the LLM per scan
    llm_revision: str = "1"    # bump when replacing weights behind the same model tag
    context_enabled: bool = True
    context_mode: str = "audit"  # off | audit | enforce (enforce arrives in M3)
    context_subject_scope: str = "top_level_directory"
    context_k: int = 3
    context_max_policy_passes: int = 4
    context_fail_closed: bool = True
    context_blur_structure: bool = True
    context_blur_stats: bool = True

    def compiled_regexes(self) -> list[re.Pattern]:
        pats = list(self.regexes)
        if self.paranoid:
            pats += PARANOID_REGEXES
        compiled = []
        for pattern in pats:
            try:
                compiled.append(re.compile(pattern))
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return compiled


def load_rules(path: str | None) -> Rules:
    rules = Rules()
    if path is None:
        return rules
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError:
        raise ValueError("invalid YAML syntax") from None
    if not isinstance(data, dict):
        raise ValueError(f"rules file must be a YAML mapping: {path}")
    allowed = {
        "keywords", "regexes", "literal_names", "literal_orgs", "org_suffixes",
        "professions", "identity_terms", "hidden_keywords", "excludes", "pinyin_allowlist",
        "use_ner", "detect_orgs", "detect_professions", "detect_en_names", "detect_pinyin",
        "detect_identity", "paranoid", "ner_engine", "use_llm", "llm_model", "llm_url",
        "llm_max", "llm_revision", "context",
    }
    if set(data) - allowed:
        raise ValueError("unknown rules key(s)")
    for key in ("keywords", "regexes", "literal_names", "literal_orgs", "org_suffixes",
                "professions", "identity_terms", "hidden_keywords", "excludes",
                "pinyin_allowlist"):
        if key in data:
            value = data[key]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"rules key {key!r} must be a list of strings")
            # user lists extend defaults rather than replacing them
            getattr(rules, key).extend(value)
    for key in ("use_ner", "detect_orgs", "detect_professions",
                "detect_en_names", "detect_pinyin", "detect_identity", "paranoid"):
        if key in data:
            value = data[key]
            if not isinstance(value, bool):
                raise ValueError(f"rules key {key!r} must be a boolean")
            setattr(rules, key, value)
    if "ner_engine" in data:
        engine = str(data["ner_engine"]).lower()
        if engine not in ("auto", "hanlp", "jieba", "off"):
            raise ValueError(f"ner_engine must be auto|hanlp|jieba|off, got {engine!r}")
        rules.ner_engine = engine
    if "use_llm" in data:
        value = data["use_llm"]
        if not isinstance(value, bool):
            raise ValueError("rules key 'use_llm' must be a boolean")
        rules.use_llm = value
    if "llm_model" in data:
        value = data["llm_model"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("rules key 'llm_model' must be a non-empty string")
        rules.llm_model = value
    if "llm_url" in data:
        value = data["llm_url"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("rules key 'llm_url' must be a non-empty string")
        rules.llm_url = value
    if "llm_max" in data:
        value = data["llm_max"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("rules key 'llm_max' must be a non-negative integer")
        rules.llm_max = value
    if "llm_revision" in data:
        value = data["llm_revision"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("rules key 'llm_revision' must be a non-empty string")
        rules.llm_revision = value
    if "context" in data:
        context = data["context"]
        if not isinstance(context, dict):
            raise ValueError("rules key 'context' must be a mapping")
        allowed_context = {
            "enabled", "mode", "subject_scope", "k_anonymity",
            "max_policy_passes", "fail_closed", "blur_structure", "blur_stats",
        }
        unknown_context = sorted(set(context) - allowed_context)
        if unknown_context:
            raise ValueError(
                f"unknown context key(s): {', '.join(unknown_context)}"
            )
        if "enabled" in context:
            value = context["enabled"]
            if not isinstance(value, bool):
                raise ValueError("context key 'enabled' must be a boolean")
            rules.context_enabled = value
        if "mode" in context:
            value = context["mode"]
            if value not in ("off", "audit", "enforce"):
                raise ValueError("context key 'mode' must be off|audit|enforce")
            rules.context_mode = value
        if "subject_scope" in context:
            value = context["subject_scope"]
            if value not in ("top_level_directory", "whole_release"):
                raise ValueError(
                    "context key 'subject_scope' must be "
                    "top_level_directory|whole_release"
                )
            rules.context_subject_scope = value
        if "k_anonymity" in context:
            value = context["k_anonymity"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("context key 'k_anonymity' must be a positive integer")
            rules.context_k = value
        if "max_policy_passes" in context:
            value = context["max_policy_passes"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    "context key 'max_policy_passes' must be a positive integer"
                )
            rules.context_max_policy_passes = value
        for key, attr in (
            ("fail_closed", "context_fail_closed"),
            ("blur_structure", "context_blur_structure"),
            ("blur_stats", "context_blur_stats"),
        ):
            if key in context:
                value = context[key]
                if not isinstance(value, bool):
                    raise ValueError(f"context key {key!r} must be a boolean")
                setattr(rules, attr, value)
    # Validate user-supplied regexes while errors can still be reported as a
    # bad rules file by the CLI, rather than later during the scan pipeline.
    rules.compiled_regexes()
    return rules
