"""从种子名称中提取媒体标题（番名/剧名）的工具模块。

支持常见的 PT 站点命名规则：
- 【字幕组】番名 S01E01 1080p
- [字幕组] 番名 第01集
- 番名.S01E01.1080p.BluRay
- 番名 (2024) S01E01
- 番名 - 01 (1080p)

算法：移除字幕组前缀、集数标记、分辨率/编码等后缀，提取核心标题。
"""

import re

# 字幕组前缀：【...】或 [...]，支持嵌套
_SUBGROUP_PATTERN = re.compile(
    r'^(?:【[^】]*】|\[[^\]]*\])\s*',
    re.UNICODE,
)

# 年份标记：(2024) 或 .2024. 或 (2024-2025) 等
_YEAR_PATTERN = re.compile(
    r'[\s._]*(?:\(?\d{4}(?:[-–]\d{4})?\)?)[\s._]*',
    re.UNICODE,
)

# 集数标记（多种格式）
_EPISODE_PATTERNS = [
    # S01E01, S1E1, S01E01E02 (多集)
    re.compile(r'[\s._-]*S\d+E\d+(?:E\d+)*', re.IGNORECASE),
    # EP01, Ep.01 (需要 EP 或 Ep 前缀)
    re.compile(r'[\s._-]*(?:EP|Ep\.?)\s*\.?\d+(?:\s*[-–]\s*\d+)?', re.IGNORECASE),
    # 独立的 E01 格式（E 后跟数字，用 word boundary 避免匹配单词中的 e）
    re.compile(r'\bE\s*\.?\d+(?:\s*[-–]\s*\d+)?\b'),
    # 第01集, 第1话, 第01话
    re.compile(r'[\s._-]*第\s*\d+\s*[集话話回]'),
    # - 01, - 01-12 (横杠后跟数字)
    re.compile(r'[\s._-]+-\s*\d+(?:\s*[-–]\s*\d+)?'),
    # 纯数字集数（需在标题后，如 "番名 01" 或 "番名.01"）
    re.compile(r'[\s._]+\d{1,4}(?:\s*[-–]\s*\d{1,4})?(?=[\s._]|$)'),
]

# 分辨率/编码/来源等后缀
_QUALITY_SUFFIX_PATTERNS = [
    # 分辨率
    re.compile(r'[\s._-]*(?:2160p|1080p|720p|480p|4K|UHD)', re.IGNORECASE),
    # 编码
    re.compile(r'[\s._-]*(?:x264|x265|H\.?264|H\.?265|HEVC|AVC|AAC|FLAC|DTS|AC3)', re.IGNORECASE),
    # 来源
    re.compile(r'[\s._-]*(?:BluRay|BDRip|WEB-?DL|WEB-?Rip|HDTV|HDRip|DVDRip|Remux|AMZN|NF|Netflix)', re.IGNORECASE),
    # 色彩空间
    re.compile(r'[\s._-]*(?:HDR|HDR10|HDR10\+|DV|DoVi|SDR|HLG)', re.IGNORECASE),
    # 位深
    re.compile(r'[\s._-]*10bit', re.IGNORECASE),
    # 语言标记（简中/繁中/日语/英语/双语/多语等）
    re.compile(r'[\s._-]*(?:简[体中]?[日英]?|繁[体中]?[日英]?|[日英]语|[日英][中字]|中[日英]?[字幕]?|双语|多语|简繁|中日|中英)[\s._-]*(?:字幕|配音|内嵌|外挂)?', re.IGNORECASE),
    # 发布组（末尾的 -GROUP 或 [GROUP]）
    re.compile(r'[\s._-]*\[[^\]]*\]\s*$'),
    re.compile(r'[\s._-]*-[A-Za-z0-9_-]+\s*$'),
    # 文件扩展名
    re.compile(r'\.(?:mkv|mp4|avi|rmvb|ts|flv|wmv)$', re.IGNORECASE),
    # 剩余的方括号内容
    re.compile(r'\[[^\]]*\]'),
    # 剩余的空括号
    re.compile(r'\(\s*\)'),
]

# 连续标点/空格清理
_MULTI_PUNCT = re.compile(r'[\s._]{2,}')
_TRAILING_PUNCT = re.compile(r'[\s._-]+$')
_LEADING_PUNCT = re.compile(r'^[\s._-]+')

# 纯质量/编码标记（用于判断提取结果是否有效）
_QUALITY_ONLY_PATTERN = re.compile(
    r'^(?:\d+p|4K|UHD|10bit|HDR|HDR10\+?|SDR|HEVC|AVC|x264|x265|H\.?264|H\.?265|AAC|FLAC|DTS|AC3|BluRay|BDRip|WEB-?DL|WEB-?Rip|HDTV|DVDRip|Remux|[A-Z]{2,})+$',
    re.IGNORECASE,
)


def extract_media_title(name: str, strip_quality: bool = True) -> str:
    """从种子名称中提取媒体标题。

    Args:
        name: 种子名称
        strip_quality: 是否移除质量/编码后缀（默认 True）

    Returns:
        提取后的媒体标题；如果提取失败，返回空字符串。
    """
    if not name:
        return ""

    title = name.strip()

    # 1. 移除字幕组前缀
    title = _SUBGROUP_PATTERN.sub('', title)

    # 2. 移除质量/编码后缀（从后往前移除，避免误伤标题）
    if strip_quality:
        for pat in _QUALITY_SUFFIX_PATTERNS:
            title = pat.sub('', title)

    # 3. 移除集数标记
    for pat in _EPISODE_PATTERNS:
        title = pat.sub('', title)

    # 4. 移除年份（但保留标题中的数字，如 "24年组"）
    # 只移除独立的年份标记，不移除标题中的数字
    title = _YEAR_PATTERN.sub(' ', title)

    # 5. 清理标点
    title = _MULTI_PUNCT.sub(' ', title)
    title = _TRAILING_PUNCT.sub('', title)
    title = _LEADING_PUNCT.sub('', title)
    title = title.strip()

    # 6. 如果清理后为空，返回空字符串
    if not title:
        return ""

    # 7. 如果结果只是数字/标点，返回空字符串
    if re.match(r'^[\d\s._\-]+$', title):
        return ""

    # 8. 如果结果只是质量/编码标记，返回空字符串（无法提取有效标题）
    if _QUALITY_ONLY_PATTERN.match(title):
        return ""

    return title


def normalize_title(title: str) -> str:
    """将标题归一化用于分组比较（小写、去空格标点）。

    Args:
        title: 媒体标题

    Returns:
        归一化后的标题
    """
    if not title:
        return ""
    # 小写 + 去除所有空格和标点
    normalized = re.sub(r'[\s._\-]', '', title.lower())
    return normalized
