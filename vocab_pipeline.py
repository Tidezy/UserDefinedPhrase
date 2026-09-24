from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pypinyin import Style, lazy_pinyin

BASE = Path(__file__).resolve().parent
DEFAULT_SOURCE = BASE / "UserDefinedPhrase.txt"
DEFAULT_OUTPUT = BASE / "UserDefinedPhrase.dat"
DEFAULT_CANDIDATES = BASE / "在线候选词.txt"
DEFAULT_REPORT = BASE / "词库更新报告.json"
DEFAULT_CONFIG = BASE / "vocab_sources.json"
APPROVED = BASE / "approved_terms.txt"

MAGIC = b"mschxudp"
SELF_STUDY_MAGIC = bytes.fromhex("55aa88810200600055aa55aa")
SELF_STUDY_BASE = 0x2400
SELF_STUDY_ENTRY_SIZE = 60
PY_RE = re.compile(r"^[a-z]+$")

# Win10/Win11 Microsoft Pinyin self-study format syllable table.
# The self-study DAT stores a syllable index for each character, not a custom trigger.
SELF_STUDY_PINYINS = """
a ai an ang ao ba bai ban bang bao bei ben beng bi bian biao bie bin bing bo bu
ca cai can cang cao ce cen ceng cha chai chan chang chao che chen cheng chi
chong chou chu chua chuai chuan chuang chui chun chuo ci cong cou cu cuan cui cun cuo
da dai dan dang dao de dei den deng di dia dian diao die ding diu dong dou du duan
dui dun duo e ei en eng er fa fan fang fei fen feng fiao fo fou fu ga gai gan gang gao
ge gei gen geng gong gou gu gua guai guan guang gui gun guo ha hai han hang hao he hei
hen heng hong hou hu hua huai huan huang hui hun huo ji jia jian jiang jiao jie jin
jing jiong jiu ju juan jue jun ka kai kan kang kao ke kei ken keng kong kou ku kua
kuai kuan kuang kui kun kuo la lai lan lang lao le lei leng li lia lian liang liao lie
lin ling liu lo long lou lu luan lue lun luo lv ma mai man mang mao me mei men meng mi
mian miao mie min ming miu mo mou mu na nai nan nang nao ne nei nen neng ni nian niang
niao nie nin ning niu nong nou nu nuan nue nun nuo nv o ou pa pai pan pang pao pei pen
peng pi pian piao pie pin ping po pou pu qi qia qian qiang qiao qie qin qing qiong qiu
qu quan que qun ran rang rao re ren reng ri rong rou ru rua ruan rui run ruo sa sai san
sang sao se sen seng sha shai shan shang shao she shei shen sheng shi shou shu shua
shuai shuan shuang shui shun shuo si song sou su sui sun suo ta tai tan tang tao te
tei teng ti tian tiao tie ting tong tou tu tuan tui tun tuo wa wai wan wang wei wen
weng wo wu xi xia xian xiang xiao xie xin xing xiong xiu xu xuan xue xun ya yan yang
yao ye yi yin ying yo yong you yu yuan yue yun za zai zan zang zao ze zei zen zeng
zha zhai zhan zhang zhao zhe zhei zhen zheng zhi zhong zhou zhu zhua zhuai zhuan
zhuang zhui zhun zhuo zi zong zou zu zuan zui zun zuo
""".split()
SELF_STUDY_PINYIN_INDEX = {pinyin: index for index, pinyin in enumerate(SELF_STUDY_PINYINS)}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def is_cjk_phrase(phrase: str) -> bool:
    return bool(phrase) and all("\u4e00" <= char <= "\u9fff" for char in phrase)


def normalize_phrase(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    value = value.replace("\u00a0", " ")
    return value


def generated_pinyin(phrase: str) -> str:
    parts = lazy_pinyin(phrase, style=Style.NORMAL, errors="default")
    pinyin = "".join(parts).lower().replace("'", "")
    return "".join(char for char in pinyin if "a" <= char <= "z")


def valid_pinyin(pinyin: str) -> bool:
    # 微软拼音用户词库的快捷码不能以 u/v 开头。
    return bool(pinyin) and len(pinyin) <= 32 and bool(PY_RE.fullmatch(pinyin)) and pinyin[0] not in "uv"


def parse_source(path: Path) -> list[tuple[str, str, int, str]]:
    """读取源文件，返回 pinyin, phrase, position, source line。"""
    result: list[tuple[str, str, int, str]] = []
    if not path.exists():
        return result

    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        try:
            if "\t" in line:
                parts = [part.strip() for part in line.split("\t")]
                if len(parts) < 2:
                    continue
                pinyin = parts[0].lower()
                phrase = normalize_phrase(parts[1])
                position = int(parts[2]) if len(parts) >= 3 and parts[2] else 1
            else:
                phrase = normalize_phrase(line)
                pinyin = generated_pinyin(phrase) if is_cjk_phrase(phrase) else ""
                position = 0

            if not phrase or len(phrase) > 64 or not (0 <= position <= 9):
                continue
            if not valid_pinyin(pinyin):
                continue
            result.append((pinyin, phrase, position, f"{path.name}:{line_number}"))
        except (TypeError, ValueError):
            continue
    return result


def load_local_sources(config: dict) -> list[tuple[str, str, int, str]]:
    names = config.get("local_sources", ["ChsPinyinUDL.txt"])
    records: list[tuple[str, str, int, str]] = []
    for name in names:
        path = BASE / name
        records.extend(parse_source(path))
    return records


def deduplicate(records: list[tuple[str, str, int, str]]) -> list[tuple[str, str, int, str]]:
    by_key: dict[tuple[str, int], tuple[str, str, int, str]] = {}
    output: list[tuple[str, str, int, str]] = []

    # 显式指定的快捷码优先，避免自动全拼占用同一个候选位置。
    for pinyin, phrase, position, origin in records:
        if not position:
            continue
        key = (pinyin, position)
        if key in by_key:
            continue
        record = (pinyin, phrase, position, origin)
        by_key[key] = record
        output.append(record)

    # 未显式指定位置的词条，按拼音自动分配 1—9 候选位置。
    for pinyin, phrase, position, origin in records:
        if position:
            continue
        candidate = 1
        while (pinyin, candidate) in by_key and candidate < 9:
            candidate += 1
        if candidate > 9:
            continue
        record = (pinyin, phrase, candidate, origin)
        by_key[(pinyin, candidate)] = record
        output.append(record)
    return output


def make_entry(pinyin: str, phrase: str, position: int) -> bytes:
    pinyin_bytes = pinyin.encode("utf-16-le") + b"\x00\x00"
    phrase_bytes = phrase.encode("utf-16-le") + b"\x00\x00"
    phrase_start = 16 + len(pinyin_bytes)
    return (
        bytes([0x10, 0x00, 0x10, 0x00])
        + struct.pack("<H", phrase_start)
        + bytes([position, 0x06, 0x00, 0x00, 0x00, 0x00])
        + struct.pack("<I", 0xBEEFCAFE)
        + pinyin_bytes
        + phrase_bytes
    )


def make_dat(records: list[tuple[str, str, int, str]]) -> bytes:
    entries = [make_entry(pinyin, phrase, position) for pinyin, phrase, position, _ in records]
    count = len(entries)
    items_start = 0x40 + 4 * count
    total_size = items_start + sum(len(entry) for entry in entries)
    timestamp = int(time.time())

    result = bytearray([
        0x6D, 0x73, 0x63, 0x68, 0x78, 0x75, 0x64, 0x70,
        0x02, 0x00, 0x60, 0x00, 0x01, 0x00, 0x00, 0x00,
        0x40, 0x00, 0x00, 0x00,
    ])
    result += struct.pack("<I", items_start)
    result += struct.pack("<I", total_size)
    result += struct.pack("<I", count)
    result += struct.pack("<I", timestamp)
    result += b"\x00" * 28

    offset = 0
    for entry in entries:
        result += struct.pack("<I", offset)
        offset += len(entry)
    for entry in entries:
        result += entry
    return bytes(result)


def self_study_records(records: list[tuple[str, str, int, str]]) -> list[tuple[str, list[str]]]:
    """将词源转换成自学习格式可用的逐字拼音词条。

    自学习格式没有自定义触发码和候选位置，因此只接收 2—12 个汉字的纯中文词条。
    """
    result: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for _, phrase, _, _ in records:
        if not is_cjk_phrase(phrase) or not (2 <= len(phrase) <= 12) or phrase in seen:
            continue
        pinyins = [pinyin.lower().replace("'", "") for pinyin in lazy_pinyin(phrase, style=Style.NORMAL, errors="default")]
        if len(pinyins) != len(phrase) or any(pinyin not in SELF_STUDY_PINYIN_INDEX for pinyin in pinyins):
            continue
        seen.add(phrase)
        result.append((phrase, pinyins))
    return result


def make_self_study_entry(word: str, pinyins: list[str], index: int) -> bytes:
    if not (2 <= len(word) <= 12) or len(pinyins) != len(word):
        raise ValueError("self-study word must contain 2-12 characters and one pinyin per character")
    entry = bytearray(SELF_STUDY_ENTRY_SIZE)
    struct.pack_into("<H", entry, 0, (index + 0x6D1B) & 0xFFFF)
    entry[2:12] = bytes([0x1A, 0x26, 0x00, 0x00, 0x00, 0x00, 0x00, 0x04, len(word), 0x5A])
    word_bytes = word.encode("utf-16-le")
    entry[12:12 + len(word_bytes)] = word_bytes
    pinyin_offset = 12 + len(word_bytes)
    for position, pinyin in enumerate(pinyins):
        struct.pack_into("<H", entry, pinyin_offset + position * 2, SELF_STUDY_PINYIN_INDEX[pinyin])
    return bytes(entry)


def make_self_study_dat(records: list[tuple[str, list[str]]]) -> bytes:
    if len(records) > 20000:
        raise ValueError("Microsoft Pinyin self-study DAT supports at most 20000 entries")
    # DateTime.Now.Ticks is the format used by the Windows exporter.
    ticks = (621355968000000000 + int(time.time() * 10_000_000)) & 0xFFFFFFFF
    result = bytearray(SELF_STUDY_MAGIC)
    result += struct.pack("<Q", len(records))
    result += struct.pack("<I", ticks)
    result += b"\x00" * (SELF_STUDY_BASE - len(result))
    result += b"".join(make_self_study_entry(word, pinyins, index)
                        for index, (word, pinyins) in enumerate(records))
    aligned_size = (len(result) + 1023) // 1024 * 1024
    result += b"\x00" * (aligned_size - len(result))
    return bytes(result)


def parse_self_study_dat(data: bytes) -> list[tuple[str, list[str]]]:
    if len(data) < SELF_STUDY_BASE or data[:12] != SELF_STUDY_MAGIC:
        raise ValueError("self-study DAT magic/header is invalid")
    count = struct.unpack_from("<I", data, 12)[0]
    if struct.unpack_from("<I", data, 16)[0] != 0 or count > 20000:
        raise ValueError("self-study DAT count is invalid")
    if SELF_STUDY_BASE + count * SELF_STUDY_ENTRY_SIZE > len(data):
        raise ValueError("self-study DAT entry area is truncated")
    result: list[tuple[str, list[str]]] = []
    for index in range(count):
        start = SELF_STUDY_BASE + index * SELF_STUDY_ENTRY_SIZE
        word_length = data[start + 10]
        if not (2 <= word_length <= 12):
            raise ValueError("self-study DAT word length is invalid")
        word_bytes = data[start + 12:start + 12 + word_length * 2]
        word = word_bytes.decode("utf-16-le")
        if not is_cjk_phrase(word):
            raise ValueError("self-study DAT contains a non-Chinese word")
        pinyin_offset = start + 12 + word_length * 2
        pinyins: list[str] = []
        for position in range(word_length):
            pinyin_index = struct.unpack_from("<H", data, pinyin_offset + position * 2)[0]
            if pinyin_index >= len(SELF_STUDY_PINYINS):
                raise ValueError("self-study DAT pinyin index is invalid")
            pinyins.append(SELF_STUDY_PINYINS[pinyin_index])
        result.append((word, pinyins))
    return result


def validate_self_study_dat(path: Path) -> list[tuple[str, list[str]]]:
    return parse_self_study_dat(path.read_bytes())


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(data)
    temp_path.replace(path)


def parse_dat(data: bytes) -> list[tuple[str, str, int]]:
    if len(data) < 64 or data[:8] != MAGIC:
        raise ValueError("DAT magic/header is invalid")
    if data[8:20] != bytes([0x02, 0x00, 0x60, 0x00, 0x01, 0x00, 0x00, 0x00, 0x40, 0x00, 0x00, 0x00]):
        raise ValueError("DAT fixed header is invalid")
    items_start = struct.unpack_from("<I", data, 20)[0]
    total_size = struct.unpack_from("<I", data, 24)[0]
    count = struct.unpack_from("<I", data, 28)[0]
    if total_size != len(data) or items_start != 0x40 + 4 * count:
        raise ValueError("DAT length/offset table is invalid")
    if data[36:64] != b"\x00" * 28:
        raise ValueError("DAT reserved header is invalid")

    offsets = [struct.unpack_from("<I", data, 64 + i * 4)[0] for i in range(count)]
    if not offsets or offsets[0] != 0:
        raise ValueError("DAT offset table is invalid")

    result: list[tuple[str, str, int]] = []
    for index, offset in enumerate(offsets):
        end = offsets[index + 1] if index + 1 < count else len(data) - items_start
        if end < offset or items_start + end > len(data):
            raise ValueError("DAT entry boundary is invalid")
        entry = data[items_start + offset:items_start + end]
        if len(entry) < 24 or entry[:4] != bytes([0x10, 0x00, 0x10, 0x00]):
            raise ValueError("DAT entry header is invalid")
        phrase_start = entry[4]
        position = entry[6]
        if entry[5] != 0 or entry[7] != 0x06 or entry[8:12] != b"\x00" * 4:
            raise ValueError("DAT entry metadata is invalid")
        if not (1 <= position <= 9) or phrase_start < 16 or phrase_start > len(entry):
            raise ValueError("DAT entry offset is invalid")
        pinyin = entry[16:phrase_start].decode("utf-16-le").rstrip("\x00")
        phrase = entry[phrase_start:].decode("utf-16-le").rstrip("\x00")
        if not valid_pinyin(pinyin) or not phrase or len(phrase) > 64:
            raise ValueError("DAT entry text is invalid")
        result.append((pinyin, phrase, position))
    return result


def validate_dat(path: Path) -> list[tuple[str, str, int]]:
    return parse_dat(path.read_bytes())


def build(config: dict, output: Path = DEFAULT_OUTPUT, include_candidates: bool = False) -> dict:
    records = load_local_sources(config)
    source_names = config.get("local_sources", [])
    if include_candidates:
        records.extend(parse_source(DEFAULT_CANDIDATES))
    records = deduplicate(records)
    data = make_dat(records)
    atomic_write(output, data)
    parsed = validate_dat(output)
    return {
        "output": str(output),
        "source_files": source_names,
        "entries": len(parsed),
        "bytes": len(data),
        "included_candidates": include_candidates,
    }


def discover(config: dict, offline: bool = False) -> dict:
    candidate_path = DEFAULT_CANDIDATES
    result: dict = {"url_sources": 0, "candidates": 0, "warnings": []}
    if offline:
        result["warnings"].append("offline mode: 使用已有候选文件")
    else:
        found: list[tuple[int, str, str]] = []
        for source in config.get("sources", []):
            if not source.get("enabled", False):
                continue
            name = source.get("name", "unnamed")
            url = source.get("url", "")
            try:
                request = Request(url, headers={"User-Agent": "MS-Pinyin-Vocab-Pipeline/1.0"})
                with urlopen(request, timeout=int(source.get("timeout", 90))) as response:
                    raw = response.read()
                result["url_sources"] += 1
                source_format = source.get("format", "jieba")
                min_frequency = int(source.get("min_frequency", 1000))
                max_length = int(source.get("max_length", 8))
                max_candidates = int(source.get("max_candidates", 2000))
                parsed: list[tuple[int, str, str]] = []
                for line in raw.decode("utf-8", errors="ignore").splitlines():
                    parts = line.split()
                    if not parts:
                        continue
                    if source_format == "jieba" and len(parts) >= 2:
                        word = parts[0]
                        try:
                            frequency = int(parts[1])
                        except ValueError:
                            continue
                    else:
                        word = parts[0]
                        frequency = min_frequency
                    if frequency < min_frequency or not (2 <= len(word) <= max_length) or not is_cjk_phrase(word):
                        continue
                    parsed.append((frequency, word, name))
                parsed.sort(reverse=True)
                found.extend(parsed[:max_candidates])
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                result["warnings"].append(f"{name}: {exc}")

        if result["url_sources"]:
            # 候选文件只用于审阅，不默认塞入 DAT，避免把基础词典重新导入。
            lines = [
                "# 在线候选词：自动收集，仅供审阅；默认不会导入 DAT。",
                "# 格式：每行一个中文词语。确认后复制到 approved_terms.txt。",
            ]
            seen: set[str] = set()
            for _, word, source in sorted(found, key=lambda x: (-x[0], x[1])):
                if word not in seen:
                    seen.add(word)
                    lines.append(word)
            candidate_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result["candidates"] = len(seen)

    if candidate_path.exists():
        result["candidates"] = sum(1 for line in candidate_path.read_text(encoding="utf-8-sig").splitlines() if line and not line.startswith("#"))
    return result


def read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_report(report: dict, path: Path) -> None:
    report = {"generated_at": now_iso(), **report}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows 11 微软拼音扩展词库流水线")
    parser.add_argument("command", choices=("build", "update", "validate", "approve"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--offline", action="store_true", help="不访问网络，仅使用已有候选文件")
    parser.add_argument("--include-candidates", action="store_true", help="将在线候选词也导入 DAT；默认关闭")
    parser.add_argument("--approve-file", type=Path, help="approve 命令使用的候选词文件")
    args = parser.parse_args()

    try:
        config = read_config(args.config)
        if args.command == "validate":
            entries = validate_dat(args.output)
            print(f"用户自定义短语校验通过：{len(entries)} 条，{args.output}")
            return 0

        if args.command == "approve":
            source = args.approve_file or DEFAULT_CANDIDATES
            approved_lines = APPROVED.read_text(encoding="utf-8-sig").splitlines() if APPROVED.exists() else []
            existing = {line.strip() for line in approved_lines if line.strip() and not line.startswith("#")}
            added = 0
            for line in source.read_text(encoding="utf-8-sig").splitlines():
                word = line.strip()
                if word and not word.startswith("#") and is_cjk_phrase(word) and word not in existing:
                    approved_lines.append(word)
                    existing.add(word)
                    added += 1
            APPROVED.write_text("\n".join(approved_lines).rstrip() + "\n", encoding="utf-8")
            print(f"已批准 {added} 个候选词到 {APPROVED.name}")
            return 0

        discovery = discover(config, offline=args.offline) if args.command == "update" else {"candidates": 0, "url_sources": 0, "warnings": []}
        include_candidates = args.include_candidates or bool(config.get("include_candidates_in_dat", False))
        result = build(config, output=args.output, include_candidates=include_candidates)
        report = {**discovery, **result}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if discovery.get("warnings"):
            print("警告：在线来源不可用时，仍使用本地词源完成构建。", file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"流水线失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
