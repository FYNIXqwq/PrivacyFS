"""Pinyin name detection: ZhangSan / zhangsan / Zhang San / Zhang_San /
San Zhang / Zhang-San ...

Strategy (recall comes from structure, not enumeration):
- surname set = existing Chinese surname list converted to pinyin via
  pypinyin, with heteronyms (曾 -> ceng/zeng, 单 -> dan/chan/shan, ...)
- given names validated against the canonical ~410 pinyin syllable table
- three surface forms: separated (space/underscore/dot/dash), camelCase,
  and bare concatenated (surname-first only, length >= 4)
- a small blocklist keeps common English words that segment like pinyin
  ("lime" = li+me, "data" = da+ta) from being masked
"""

from __future__ import annotations

import itertools
import re

from .keywords import Finding

_TOKEN = re.compile(r"[A-Za-z]+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_HAS_LETTER = re.compile(r"[A-Za-z]")

# canonical tone-free pinyin syllables
_SYLLABLES = frozenset(
    "a ai an ang ao "
    "ba bai ban bang bao bei ben beng bi bian biao bie bin bing bo bu "
    "ca cai can cang cao ce cen ceng cha chai chan chang chao che chen cheng "
    "chi chong chou chu chua chuai chuan chuang chui chun chuo ci cong cou "
    "cu cuan cui cun cuo "
    "da dai dan dang dao de dei den deng di dia dian diao die ding diu dong "
    "dou du duan dui dun duo "
    "e ei en eng er "
    "fa fan fang fei fen feng fo fou fu "
    "ga gai gan gang gao ge gei gen geng gong gou gu gua guai guan guang gui "
    "gun guo "
    "ha hai han hang hao he hei hen heng hong hou hu hua huai huan huang hui "
    "hun huo "
    "ji jia jian jiang jiao jie jin jing jiong jiu ju juan jue jun "
    "ka kai kan kang kao ke kei ken keng kong kou ku kua kuai kuan kuang kui "
    "kun kuo "
    "la lai lan lang lao le lei leng li lia lian liang liao lie lin ling liu "
    "long lou lu lv luan lve lun luo "
    "ma mai man mang mao me mei men meng mi mian miao mie min ming miu mo mou mu "
    "na nai nan nang nao ne nei nen neng ni nian niang niao nie nin ning niu "
    "nong nou nu nv nuan nve nun nuo "
    "o ou "
    "pa pai pan pang pao pei pen peng pi pian piao pie pin ping po pou pu "
    "qi qia qian qiang qiao qie qin qing qiong qiu qu quan que qun "
    "ran rang rao re ren reng ri rong rou ru ruan rui run ruo "
    "sa sai san sang sao se sen seng sha shai shan shang shao she shei shen "
    "sheng shi shou shu shua shuai shuan shuang shui shun shuo si song sou "
    "su suan sui sun suo "
    "ta tai tan tang tao te tei teng ti tian tiao tie ting tong tou tu tuan "
    "tui tun tuo "
    "wa wai wan wang wei wen weng wo wu "
    "xi xia xian xiang xiao xie xin xing xiong xiu xu xuan xue xun "
    "ya yan yang yao ye yi yin ying yo yong you yu yuan yue yun "
    "za zai zan zang zao ze zei zen zeng zha zhai zhan zhang zhao zhe zhei "
    "zhen zheng zhi zhong zhou zhu zhua zhuai zhuan zhuang zhui zhun zhuo zi "
    "zong zou zu zuan zui zun zuo".split()
)

# common English words that segment like pinyin names
_DEFAULT_BLOCKLIST = frozenset({"lime", "lite", "data", "luna"})

# Ordinary English words that happen to be surname + 1-2 pinyin syllables.
# Apply to individual tokens, not whole filenames or directories. Separately
# written names such as Li Ne / LiNe and Mo De must still be considered.
_COMMON_WORDS = frozenset(
    "license licence cache module manage machine line mode pane rename "
    "same sane shine manga lineman".split()
)

_surname_cache: frozenset[str] | None = None


def _surname_pinyin() -> frozenset[str]:
    global _surname_cache
    if _surname_cache is not None:
        return _surname_cache
    from pypinyin import Style, pinyin

    from .names import _COMPOUND_SURNAMES, _SINGLE_SURNAMES

    out: set[str] = set()

    def add(reading: str) -> None:
        r = reading.lower()
        out.add(r)
        if "ü" in r:
            out.add(r.replace("ü", "v"))
            out.add(r.replace("ü", "u"))

    for ch in _SINGLE_SURNAMES:
        for reading in pinyin(ch, style=Style.NORMAL, heteronym=True)[0]:
            add(reading)
    for comp in _COMPOUND_SURNAMES:
        for combo in itertools.product(*pinyin(comp, style=Style.NORMAL, heteronym=True)):
            add("".join(combo))
    _surname_cache = frozenset(out)
    return _surname_cache


def _syllable_count(token: str) -> int:
    """N if the token is exactly N (1 or 2) whole pinyin syllables, else 0."""
    i = n = 0
    while i < len(token):
        hit = 0
        for size in range(min(6, len(token) - i), 0, -1):
            if token[i : i + size] in _SYLLABLES:
                hit = size
                break
        if not hit:
            return 0
        i += hit
        n += 1
        if n > 2:
            return 0
    return n


class PinyinDetector:
    def __init__(self, allowlist: list[str] | None = None):
        extra = {w.lower() for w in (allowlist or [])}
        self._blocklist = _DEFAULT_BLOCKLIST | extra
        self._by_first: dict[str, list[str]] | None = None

    @property
    def surnames(self) -> frozenset[str]:
        return _surname_pinyin()

    @property
    def _by_first_letter(self) -> dict[str, list[str]]:
        """Surnames indexed by first letter, longest first -- so the concat
        check only tries a handful of candidates per token."""
        if self._by_first is None:
            idx: dict[str, list[str]] = {}
            for s in self.surnames:
                idx.setdefault(s[0], []).append(s)
            for v in idx.values():
                v.sort(key=len, reverse=True)
            self._by_first = idx
        return self._by_first

    def _blocked(self, surface: str) -> bool:
        key = re.sub(r"[ _.\-]", "", surface.lower())
        return key in self._blocklist

    def find(self, name: str) -> list[Finding]:
        if not _HAS_LETTER.search(name):
            return []
        surnames = self.surnames

        # split into letter runs, then split camelCase further; keep spans
        subs: list[tuple[str, int, int]] = []  # (lower text, start, end)
        for m in _TOKEN.finditer(name):
            tok, s = m.group(0), m.start()
            prev = 0
            for cm in _CAMEL.finditer(tok):
                subs.append((tok[prev : cm.start()].lower(), s + prev, s + cm.start()))
                prev = cm.start()
            subs.append((tok[prev:].lower(), s + prev, s + len(tok)))

        findings: list[Finding] = []
        seen: set[str] = set()

        def emit(a: int, b: int) -> None:
            surface = name[subs[a][1] : subs[b][2]]
            if surface in seen or self._blocked(surface):
                return
            seen.add(surface)
            findings.append(Finding("PERSON", surface))

        def try_concat(idx: int) -> bool:
            """Bare concatenated form inside one token: surname prefix +
            1-2 given syllables, e.g. zhangsan / Zhangsan / LISI."""
            t = subs[idx][0]
            if len(t) < 4 or t in surnames:
                return False
            for sur in self._by_first_letter.get(t[0], ()):  # first-letter index
                if len(sur) >= 2 and t.startswith(sur) and \
                        _syllable_count(t[len(sur):]) in (1, 2):
                    emit(idx, idx)
                    return True
            return False

        i = 0
        while i < len(subs):
            t = subs[i][0]
            if t in _COMMON_WORDS:
                i += 1
                continue
            if len(t) >= 2 and t in surnames:
                # surname-first: take 1-2 given syllable tokens
                # (separated or camel: Zhang San / ZhangSan / Zhang XiaoMing)
                j, taken = i + 1, 0
                while j < len(subs) and taken < 2:
                    if subs[j][0] in _COMMON_WORDS:
                        break
                    c = _syllable_count(subs[j][0])
                    if c == 0 or (taken == 1 and c != 1):
                        break
                    taken += 1
                    j += 1
                    if c == 2:
                        break
                if taken:
                    emit(i, j - 1)
                    i = j
                    continue
                i += 1
                continue
            # surname-last: "San Zhang" (given token must be 1-2 syllables)
            if _syllable_count(t) in (1, 2) and i + 1 < len(subs) \
                    and len(subs[i + 1][0]) >= 2 and subs[i + 1][0] in surnames:
                emit(i, i + 1)
                i += 2
                continue
            if try_concat(i):
                i += 1
                continue
            i += 1
        return findings
