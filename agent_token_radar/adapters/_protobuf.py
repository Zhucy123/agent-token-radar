"""Minimal protobuf wire-format reader (no schema, no dependency).

Google 的 Antigravity / Gemini 会话库把每次生成的用量写在 protobuf BLOB 里，
且没有公开 schema —— 字段号是靠逆向得到的。这里只实现解析所需的三个原语：

    _parse_proto_fields(data) -> [(field_num, wire_type, value)]

wire_type: 0=varint, 1=64bit, 2=length-delimited, 5=32bit。
遇到截断/非法数据立即停止并返回已解析部分（绝不抛异常），
因为调用方一律按「宁可丢也不能记错」处理：解析不出来就不记。

字段号参考（逆向自 tokei，本机实测复核）：
    gen_metadata.data
      1  = 单次生成记录
      19 = 模型名
      4  = 用量 { 2:输入(不含缓存), 3:输出(含思考), 5:缓存读, 9:思考, 10:回复 }
      11 = 首字延迟 Duration { 1:秒, 2:纳秒 }
      12 = 流式时长 Duration
      9→4→1 = 生成开始时间(秒)
"""

from __future__ import annotations

from typing import Iterator

__all__ = ["parse_proto_fields", "proto_duration_seconds", "decode_varint"]

_MAX_FIELD_INDEX = 1 << 24  # 防御性上限，避免坏数据把循环拖死


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Read one base-128 varint. Returns ``(value, new_offset)``."""
    result = 0
    shift = 0
    pos = offset
    n = len(data)
    while pos < n:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 70:  # malformed varint
            raise ValueError("varint too long")
    raise ValueError("unexpected end of varint")


def parse_proto_fields(data: bytes) -> list[tuple[int, int, object]]:
    """Parse a protobuf message into ``(field_number, wire_type, value)``.

    Returns whatever was successfully decoded before any error; never raises.
    """
    out: list[tuple[int, int, object]] = []
    pos = 0
    n = len(data)
    while pos < n:
        try:
            key, pos = decode_varint(data, pos)
        except ValueError:
            break
        field_num = key >> 3
        wire_type = key & 0x7
        if field_num <= 0 or field_num > _MAX_FIELD_INDEX:
            break
        try:
            if wire_type == 0:  # varint
                value, pos = decode_varint(data, pos)
            elif wire_type == 1:  # 64-bit
                if pos + 8 > n:
                    break
                value = data[pos : pos + 8]
                pos += 8
            elif wire_type == 2:  # length-delimited
                length, pos = decode_varint(data, pos)
                if length < 0 or pos + length > n:
                    break
                value = data[pos : pos + length]
                pos += length
            elif wire_type == 5:  # 32-bit
                if pos + 4 > n:
                    break
                value = data[pos : pos + 4]
                pos += 4
            else:
                break  # 3/4 (deprecated groups) and 6/7 are invalid here
        except ValueError:
            break
        out.append((field_num, wire_type, value))
    return out


def proto_duration_seconds(data: bytes) -> float:
    """``google.protobuf.Duration`` -> float seconds."""
    seconds = 0
    nanos = 0
    for field_num, wire_type, value in parse_proto_fields(data):
        if wire_type == 0 and field_num == 1:
            seconds = int(value)  # type: ignore[arg-type]
        elif wire_type == 0 and field_num == 2:
            nanos = int(value)  # type: ignore[arg-type]
    return seconds + nanos / 1e9


def iter_varint_pairs(data: bytes) -> Iterator[tuple[int, int]]:
    """Yield ``(field_number, value)`` for varint-typed fields only."""
    for field_num, wire_type, value in parse_proto_fields(data):
        if wire_type == 0:
            yield field_num, int(value)  # type: ignore[arg-type]
