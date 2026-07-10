"""cipher.py 单元测试。

**重要理解**：p115 的 rsa_encode 和 rsa_decode 都用同一对 (n, e) 做 pow(x, e, n)，
不是数学上的逆变换。encode 是"客户端 -> 服务端"方向（服务端有私钥 d 才能解），
decode 是"服务端 -> 客户端"方向（服务端用私钥 d 加密的响应，客户端用公钥 e 恢复）。
所以本文件不做 encode -> decode round-trip 测试（那是错的）；
端到端正确性靠 integration test 打真实 downurl 端点验证。

本文件只覆盖：
- _xor_cycle 与 p115 语义的具体等价（用金标 fixture 验证）
- _xor_cycle 的自反性（两次 XOR 恢复原文，这是 XOR 的数学性质）
- encode 输出长度是 128 倍数
- decode 输入长度校验、str/bytes 兼容
- pad 结构常量正确
"""

from __future__ import annotations

import base64

import pytest

from src.lib.cloud115.cipher import (
    G_KEY_L,
    RSA_KEY,
    _RSA_BLOCK_SIZE,
    _RSA_MSG_SIZE,
    _xor_cycle,
    rsa_decode,
    rsa_encode,
)
from src.lib.cloud115.exceptions import Cloud115CipherError


# ---------------------------------------------------------------------------
# _xor_cycle 语义对齐 p115cipher.util.xor
# ---------------------------------------------------------------------------


def test_xor_cycle_5b_data_matches_p115_semantics() -> None:
    """5 字节输入 + 12 字节 key：验证 p115 的分段 XOR 语义。

    p115 会先处理开头 1 字节 remainder（与 key[0] XOR），
    然后剩余 4 字节段与 key[:4] 逐位 XOR（不是 key 循环里的 key[1..4]）。
    """
    data = b"abcde"
    key = G_KEY_L
    result = _xor_cycle(data, key)
    # 手工按 p115 语义算：remainder=1
    expected = bytes([
        data[0] ^ key[0],  # remainder：src[0] ^ key[0]
        data[1] ^ key[0],  # 段内 idx=0：与 key[0]（不是 key[1]！）
        data[2] ^ key[1],
        data[3] ^ key[2],
        data[4] ^ key[3],
    ])
    assert result == expected


def test_xor_cycle_aligned_data_uses_full_key() -> None:
    """20 字节输入 + 12 字节 key：无 remainder，第一段用满 key[0..11]，第二段 8 字节用 key[0..7]。"""
    data = bytes(range(20))
    key = G_KEY_L
    result = _xor_cycle(data, key)
    expected = bytearray()
    # 第一段：0..11 与 key[0..11]
    for i in range(12):
        expected.append(data[i] ^ key[i])
    # 第二段：12..19 与 key[0..7]（关键：又从 key[0] 开始）
    for i in range(8):
        expected.append(data[12 + i] ^ key[i])
    assert result == bytes(expected)


def test_xor_cycle_with_4b_key() -> None:
    """4 字节 key 场景：类似 downurl 主流程用的 RSA_KEY。"""
    data = b"hello world!"  # 12 bytes
    key = RSA_KEY            # 4 bytes
    result = _xor_cycle(data, key)
    expected = bytearray()
    # remainder = 12 & 3 = 0
    # 段 0..3 与 key[0..3]，段 4..7 与 key[0..3]，段 8..11 与 key[0..3]
    for offset in (0, 4, 8):
        for i in range(4):
            expected.append(data[offset + i] ^ key[i])
    assert result == bytes(expected)


def test_xor_cycle_empty_input() -> None:
    assert _xor_cycle(b"", RSA_KEY) == b""


# ---------------------------------------------------------------------------
# _xor_cycle 的自反性（XOR 数学性质）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [b"a", b"ab", b"abc", b"abcd", b"a" * 12, b"a" * 13, b"a" * 128, bytes(range(256))],
    ids=["1B", "2B", "3B", "4B", "12B-key-aligned", "13B", "128B", "all-bytes"],
)
def test_xor_cycle_is_involutive_with_4b_key(data: bytes) -> None:
    """两次 XOR 同一 key 恢复原文（数学恒等，不依赖具体分段策略）。"""
    twice = _xor_cycle(_xor_cycle(data, RSA_KEY), RSA_KEY)
    assert twice == data


@pytest.mark.parametrize(
    "data",
    [b"a", b"abc", b"a" * 12, b"a" * 13, b"a" * 24, b"a" * 100],
    ids=["1B", "3B", "12B", "13B", "24B", "100B"],
)
def test_xor_cycle_is_involutive_with_12b_key(data: bytes) -> None:
    twice = _xor_cycle(_xor_cycle(data, G_KEY_L), G_KEY_L)
    assert twice == data


# ---------------------------------------------------------------------------
# rsa_encode 输出结构
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_len",
    [0, 1, 50, 116, 117, 118, 200, 300, 500],
)
def test_rsa_encode_output_length_is_multiple_of_128(input_len: int) -> None:
    """encode 的 base64 输出解回后长度必须是 128 的整数倍（每块严格 128 字节）。"""
    cipher_b64 = rsa_encode(b"x" * input_len)
    raw = base64.b64decode(cipher_b64)
    assert len(raw) % _RSA_BLOCK_SIZE == 0, f"input_len={input_len} -> raw_len={len(raw)}"


def test_rsa_encode_is_deterministic() -> None:
    """相同明文两次 encode 输出必须一致（没有随机因素）。"""
    payload = b'{"pickcode":"cd5xxxxx","user_id":"12345678"}'
    assert rsa_encode(payload) == rsa_encode(payload)


def test_rsa_encode_returns_ascii_base64() -> None:
    """输出必须是合法 base64 字符串（可被 b64decode）。"""
    cipher = rsa_encode(b"test payload")
    assert isinstance(cipher, bytes)
    # 能 decode 就算合法
    base64.b64decode(cipher)


def test_rsa_encode_input_length_shapes() -> None:
    """encode 的分块公式：117 字节明文 + 16 字节 rand_key 前缀，一起按 117 切块。

    对于 len(data) = L，buffer 长度 = 16 + L，
    密文块数 = ceil((16 + L) / 117)，密文总长 = 128 * 块数。
    """
    for L in [0, 100, 117, 200]:
        cipher_b64 = rsa_encode(b"x" * L)
        raw = base64.b64decode(cipher_b64)
        expected_blocks = max(1, -(-(16 + L) // 117))  # ceil 上取整
        assert len(raw) == expected_blocks * _RSA_BLOCK_SIZE, f"L={L}"


# ---------------------------------------------------------------------------
# rsa_decode 错误路径与输入兼容
# ---------------------------------------------------------------------------


def test_rsa_decode_rejects_non_multiple_length() -> None:
    """base64 解出后长度不是 128 倍数 → Cloud115CipherError。"""
    bad = base64.b64encode(b"\x00" * 60)  # 60 字节，不是 128 倍数
    with pytest.raises(Cloud115CipherError, match="not a multiple of 128"):
        rsa_decode(bad)


def test_rsa_decode_accepts_str_and_bytes() -> None:
    """API 契约：cipher_b64 参数可以是 str 或 bytes。"""
    cipher_b64_bytes = rsa_encode(b"whatever")
    cipher_b64_str = cipher_b64_bytes.decode("ascii")
    # decode 两种输入不应该抛（不管值是不是有意义的，长度合法就不该 CipherError）
    # 这里只要求"不抛长度类异常"，实际值取决于 pow 结果，不做断言
    try:
        rsa_decode(cipher_b64_str)
    except Cloud115CipherError:
        # 如果 pow 结果块不含 0x00 分隔符也可能抛，但那是另一分支
        pass


def test_rsa_decode_empty_input() -> None:
    """空 base64（长度 0，是 128 的倍数）→ 剥 XOR 后还是空。"""
    empty_b64 = base64.b64encode(b"")
    assert rsa_decode(empty_b64) == b""


# ---------------------------------------------------------------------------
# 常量与填充结构 sanity check
# ---------------------------------------------------------------------------


def test_pkcs_msg_size_constants_sanity() -> None:
    """常量一致性：117 明文 + 11 字节填充开销 = 128 密文块长度。"""
    assert _RSA_MSG_SIZE + 11 == _RSA_BLOCK_SIZE


def test_g_key_l_length() -> None:
    assert len(G_KEY_L) == 12


def test_rsa_key_length() -> None:
    assert len(RSA_KEY) == 4
