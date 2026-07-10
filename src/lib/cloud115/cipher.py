"""115 downurl 接口专用的 RSA + XOR 加解密。

协议来源：反推自 p115cipher（未引入此包，仅照协议手撸）。

要点：
1. 只用 RSA-1024 + 循环 XOR + 反序 + 固定 PKCS#1 v1.5 变体填充，全程不需要 AES；
   Python 内置 `pow()` 就能算模幂，不需要 cryptography 库。
2. 明文侧的 rand_key 恒为 16 字节零串——服务端也按零串 KDF，结果收敛为常量 G_KEY_L / RSA_KEY，
   我们直接把 KDF 短路成硬编码常量，与 p115cipher 在零 rand_key 场景下的输出等价。
3. 填充是"伪 PKCS#1 v1.5"：pad 字节固定为 0x02（真 PKCS#1 v1.5 要求随机非零）。
   请勿"改良"，改了 115 服务端解不了。
"""

from __future__ import annotations

import base64
import json

from src.lib.cloud115.exceptions import Cloud115CipherError

# RSA 公钥：n（1024 位 / 128 字节），e = 65537。协议常量，勿改。
RSA_N = 0x8686980C0F5A24C4B9D43020CD2C22703FF3F450756529058B1CF88F09B8602136477198A6E2683149659BD122C33592FDB5AD47944AD1EA4D36C6B172AAD6338C3BB6AC6227502D010993AC967D1AEF00F0C8E038DE2E4D3BC2EC368AF2E9F10A6F1EDA4F7262F136420C07C331B871BF139F74F3010E3C4FE57DF3AFB71683
RSA_E = 0x10001

# 12 字节 XOR 密钥：encode 里明文第三层 XOR 用（**encode 用常量，decode 走 KDF**，非对称）。
G_KEY_L = b"\x78\x06\xad\x4c\x33\x86\x5d\x18\x4c\x01\x3f\x46"
# 4 字节 XOR 密钥：明文第一层 XOR 用；对应 rand_key=0 场景的 KDF 输出快捷值。
RSA_KEY = b"\x8d\xa5\xa5\x8d"

# 16 字节 rand_key 占位符：全零；我们始终用零 rand_key，简化最内层 4-byte key 恒等于 RSA_KEY。
_RAND_KEY_ZEROS = b"\x00" * 16

# 144 字节 KDF 表：decode 里由服务端返回的 randkey 走 KDF 生成 12-byte key_l 时用。
# 严格照抄 p115cipher/util.py 的 G_kts 表，勿动。
_G_KTS = bytes((
    0xf0, 0xe5, 0x69, 0xae, 0xbf, 0xdc, 0xbf, 0x8a, 0x1a, 0x45, 0xe8, 0xbe, 0x7d, 0xa6, 0x73, 0xb8,
    0xde, 0x8f, 0xe7, 0xc4, 0x45, 0xda, 0x86, 0xc4, 0x9b, 0x64, 0x8b, 0x14, 0x6a, 0xb4, 0xf1, 0xaa,
    0x38, 0x01, 0x35, 0x9e, 0x26, 0x69, 0x2c, 0x86, 0x00, 0x6b, 0x4f, 0xa5, 0x36, 0x34, 0x62, 0xa6,
    0x2a, 0x96, 0x68, 0x18, 0xf2, 0x4a, 0xfd, 0xbd, 0x6b, 0x97, 0x8f, 0x4d, 0x8f, 0x89, 0x13, 0xb7,
    0x6c, 0x8e, 0x93, 0xed, 0x0e, 0x0d, 0x48, 0x3e, 0xd7, 0x2f, 0x88, 0xd8, 0xfe, 0xfe, 0x7e, 0x86,
    0x50, 0x95, 0x4f, 0xd1, 0xeb, 0x83, 0x26, 0x34, 0xdb, 0x66, 0x7b, 0x9c, 0x7e, 0x9d, 0x7a, 0x81,
    0x32, 0xea, 0xb6, 0x33, 0xde, 0x3a, 0xa9, 0x59, 0x34, 0x66, 0x3b, 0xaa, 0xba, 0x81, 0x60, 0x48,
    0xb9, 0xd5, 0x81, 0x9c, 0xf8, 0x6c, 0x84, 0x77, 0xff, 0x54, 0x78, 0x26, 0x5f, 0xbe, 0xe8, 0x1e,
    0x36, 0x9f, 0x34, 0x80, 0x5c, 0x45, 0x2c, 0x9b, 0x76, 0xd5, 0x1b, 0x8f, 0xcc, 0xc3, 0xb8, 0xf5,
))

# RSA 密文块字节长度 = modulus 长度 = 128
_RSA_BLOCK_SIZE = 128
# 每个 RSA 块可编码的明文长度 = 128 - 11 字节填充开销 = 117
_RSA_MSG_SIZE = 117


def _rsa_gen_key(rand_key: bytes, sk_len: int = 4) -> bytes:
    """115 KDF：由 rand_key 和 G_KTS 表派生一段 sk_len 字节的 XOR 密钥。

    严格照抄 p115cipher/util.py::rsa_gen_key。
    非对称设计：encode 里 12-byte XOR 用固定常量 G_KEY_L；decode 里 12-byte XOR 用本函数派生。
    """
    xor_key = bytearray(sk_len)
    length = sk_len * (sk_len - 1)
    index = 0
    for i in range(sk_len):
        x = (rand_key[i] + _G_KTS[index]) & 0xFF
        xor_key[i] = _G_KTS[length] ^ x
        length -= sk_len
        index += sk_len
    return bytes(xor_key)


def _xor_cycle(data: bytes, key: bytes) -> bytes:
    """严格对齐 p115cipher.util.xor 的语义（不是简单的循环 XOR）：

    1) 若 len(data) 不是 4 的倍数，先处理开头 remainder = len(data) & 3 字节，
       与 key[:remainder] 逐字节 XOR。
    2) 剩余部分按 len(key) 字节切段（不足整段时用实际段长 s = min(len(key), remaining)），
       每段的第 i 个字节 XOR key[i]（每段都从 key[0] 重新对齐，不是 key 循环）。

    这个语义只有当 len(data) % 4 == 0 且 len(data) % len(key) == 0 时才和"循环 XOR"等价。
    加密方向必须用这个语义才能对齐服务端解密，绝不能改成循环 XOR。
    """
    result = bytearray()
    data_len = len(data)
    key_len = len(key)
    remainder = data_len & 0b11
    # 开头 remainder 字节：与 key 的前 remainder 字节逐位 XOR
    for i in range(remainder):
        result.append(data[i] ^ key[i])
    offset = remainder
    while offset < data_len:
        segment_len = min(key_len, data_len - offset)
        # 每段都从 key[0] 重新对齐（关键：不是循环 XOR）
        for i in range(segment_len):
            result.append(data[offset + i] ^ key[i])
        offset += segment_len
    return bytes(result)


def rsa_encode(data: bytes) -> bytes:
    """把明文字节编码为 downurl form 表单里的 base64 字符串。

    流程：
    1) 4 字节 XOR
    2) 反序
    3) 12 字节 XOR
    4) 前置 16 字节零 rand_key（必须整体前置一次，不是每块前置）
    5) 每 117 字节一块，PKCS#1 v1.5 变体 pad 到 128 字节
    6) RSA(m^e mod n)，密文块 128 字节
    7) 全部拼接 -> base64
    """
    step1 = _xor_cycle(data, RSA_KEY)
    step2 = step1[::-1]
    step3 = _xor_cycle(step2, G_KEY_L)
    buffer = _RAND_KEY_ZEROS + step3

    out = bytearray()
    for offset in range(0, len(buffer), _RSA_MSG_SIZE):
        chunk = buffer[offset : offset + _RSA_MSG_SIZE]
        # 伪 PKCS#1 v1.5：0x00 + 0x02*n + 0x00 + chunk，凑到 128 字节
        pad_len = 126 - len(chunk)
        padded = b"\x00" + b"\x02" * pad_len + b"\x00" + chunk
        # 每块必须严格 128 字节，否则 pow 后 to_bytes 长度对不齐拼接会错位
        assert len(padded) == _RSA_BLOCK_SIZE
        m = int.from_bytes(padded, "big")
        c = pow(m, RSA_E, RSA_N)
        out += c.to_bytes(_RSA_BLOCK_SIZE, "big")

    return base64.b64encode(bytes(out))


def rsa_decode(cipher_b64: bytes | str) -> bytes:
    """base64 密文 -> 明文字节。encode 的完全逆变换。"""
    if isinstance(cipher_b64, str):
        cipher_b64 = cipher_b64.encode("ascii")
    raw = base64.b64decode(cipher_b64)
    # 空输入合法但没有可解密内容，直接返回空 bytes
    if not raw:
        return b""
    # 长度不是 128 倍数说明密文被篡改或截断，直接抛
    if len(raw) % _RSA_BLOCK_SIZE:
        raise Cloud115CipherError(
            f"cipher length {len(raw)} is not a multiple of {_RSA_BLOCK_SIZE}"
        )

    text = bytearray()
    for offset in range(0, len(raw), _RSA_BLOCK_SIZE):
        block = raw[offset : offset + _RSA_BLOCK_SIZE]
        c = int.from_bytes(block, "big")
        m_int = pow(c, RSA_E, RSA_N)
        # to_bytes 长度用实际 bit_length 算——RSA 结果小于 modulus，前置零字节会被 int 吞掉
        byte_len = (m_int.bit_length() + 7) // 8
        m = m_int.to_bytes(byte_len, "big")
        # 剥离 PKCS#1 填充：找到首个 0x00 分隔符，取其后的原始明文块
        try:
            sep = m.index(b"\x00")
        except ValueError as exc:
            raise Cloud115CipherError("PKCS#1 separator (0x00) missing in RSA block") from exc
        text += m[sep + 1 :]

    # 前 16 字节是服务端选择的 rand_key（每次响应都可能不同），用它 KDF 派生 12-byte key_l。
    server_rand_key = bytes(text[:16])
    body = bytes(text[16:])
    # 关键：decode 的 12-byte XOR key 走 KDF（**非对称**：encode 用常量 G_KEY_L）
    key_l = _rsa_gen_key(server_rand_key, sk_len=12)
    body = _xor_cycle(body, key_l)
    body = body[::-1]
    # 最内 4-byte XOR：我们发送时用零 rand_key，服务端也据此用 RSA_KEY（KDF(zero,4) 的 shortcut）
    body = _xor_cycle(body, RSA_KEY)
    return body


def encrypt_payload(payload: dict) -> bytes:
    """把 {"pickcode": ..., "user_id": ...} 编码为 downurl form 里的 data 值。

    separators 用紧凑格式，减少字节数（服务端不校验空白，但通行做法）。
    """
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return rsa_encode(body)


def decrypt_response(cipher_b64: str) -> dict:
    """解码 downurl 响应 data 字段的 base64 密文，返回 JSON dict。

    响应结构（外层已解 JSON，取 data 字段传进来）：
        {"<file_id>": {"file_name": ..., "file_size": ..., "pick_code": ..., "sha1": ..., "url": {"url": "..."} | 0}}
    """
    plaintext = rsa_decode(cipher_b64)
    return json.loads(plaintext)
