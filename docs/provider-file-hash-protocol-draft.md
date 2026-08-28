# Media Provider 文件 Hash 约定（草案）

> 状态：已确认，作为实现依据。本文独立描述约定；实现已合入
> [Provider 协议 V2](./provider-plugin-protocol-contract-v1.md) 和对应契约测试。

## 1. 目标与边界

文件 hash 用于在不同 storage provider 之间得到稳定的媒体文件内容指纹，同时避免为每个
媒体读取整个文件。它只针对 provider 实际存储的**原始文件字节**计算，不包含文件名、路径、
`media_id`、`provider_key` 或 `storage_ref`。

该值具备以下语义：

- 相同文件字节和相同文件长度，一定得到相同结果。
- 不同文件不保证得到不同结果；未采样区域发生变化时，结果可能不变。
- 它不是完整文件校验和，也不是安全认证或防碰撞方案。需要完整性/安全保证时，应另行计算
  全文件 SHA-256 等完整摘要。

本文将用户描述的“MB”固定解释为 MiB：`1 MiB = 1,048,576` 字节，避免不同语言使用
十进制 MB 导致结果不一致。

## 2. StorageProvider 接口

`StorageProvider` 增加一个必选方法：

```python
class StorageProvider(Protocol):
    # 其他既有方法略

    def compute_file_hash(self, *, media: MediaHandle) -> str: ...
```

调用约束：

- 方法只接收 `MediaHandle`，文件读取、Range 请求和 `storage_ref` 解释全部由 provider 负责。
- 协议使用 `media.file_size_bytes` 作为本次计算的文件长度。provider 应确认存储对象的实际
  长度与之相等；不相等时必须失败，不能用另一个长度静默计算。
- 必须基于同一个稳定文件快照计算。文件在计算期间被修改、对象被替换或读取到的字节数不符
  时，不得返回混合结果，应按现有 `ProviderOperationError` 约定报错。
- 该方法是必选能力，不提供宿主回退实现。缺少方法的 provider 由协议加载校验拒绝。

## 3. 常量和区间

```text
MiB                  = 1,048,576 bytes
HEAD_TAIL_BYTES      = 3 * MiB
MIDDLE_BYTES         = 1 * MiB
FULL_HASH_THRESHOLD  = 8 * MiB
```

所有区间都使用左闭右开表示法 `[offset, offset + length)`。

## 4. 大文件算法（`size >= 8 MiB`）

### 4.1 读取头部和尾部

设 `size = media.file_size_bytes`：

```text
head = file[0 : 3 * MiB]
tail = file[size - 3 * MiB : size]

head_sha1 = SHA1(head).digest()  # 20 个原始摘要字节
tail_sha1 = SHA1(tail).digest()
```

### 4.2 根据头尾摘要选择两个中间位置

中间区域为 `[3 * MiB, size - 3 * MiB)`。将其中从头开始的完整 1 MiB 划分为槽位，尾部
不足 1 MiB 的部分不参与槽位划分：

```text
slot_count = floor((size - 6 * MiB) / MiB)
```

由于 `size >= 8 MiB`，`slot_count >= 2`。

将 SHA-1 摘要的前 8 个字节按无符号大端整数解释为种子：

```text
head_seed = uint64_be(head_sha1[0:8])
tail_seed = uint64_be(tail_sha1[0:8])
```

第一个中间槽位由头部摘要决定：

```text
slot_1 = head_seed % slot_count
```

第二个中间槽位由尾部摘要决定，并排除第一个槽位，确保两次采样不是同一块：

```text
candidate = tail_seed % (slot_count - 1)
slot_2 = candidate if candidate < slot_1 else candidate + 1
```

对应的读取区间为：

```text
middle_1_offset = 3 * MiB + slot_1 * MiB
middle_2_offset = 3 * MiB + slot_2 * MiB

middle_1 = file[middle_1_offset : middle_1_offset + MiB]
middle_2 = file[middle_2_offset : middle_2_offset + MiB]

middle_1_sha1 = SHA1(middle_1).digest()
middle_2_sha1 = SHA1(middle_2).digest()
```

### 4.3 计算最终 hash

最终摘要的输入必须使用四个 SHA-1 的**原始 20 字节摘要**，不能使用其十六进制文本。`u64be`
表示 8 字节无符号大端整数编码：

```text
payload =
    ASCII("media-file-hash-v1")
    + 0x00
    + ASCII("sampled")
    + 0x00
    + u64be(size)
    + head_sha1
    + tail_sha1
    + middle_1_sha1
    + middle_2_sha1

final_digest = SHA1(payload).hexdigest().lower()
return "media-file-hash-v1:" + final_digest
```

因此最终返回值的格式固定为：

```text
media-file-hash-v1:<40 位小写十六进制字符>
```

## 5. 小文件算法（`size < 8 MiB`）

小于 8 MiB 时无法保证头部 3 MiB、尾部 3 MiB 和两个互不重叠的 1 MiB 中间块同时存在，
因此直接计算全文 SHA-1，再使用同一输出格式封装：

```text
full_sha1 = SHA1(file[0 : size]).digest()

payload =
    ASCII("media-file-hash-v1")
    + 0x00
    + ASCII("full")
    + 0x00
    + u64be(size)
    + full_sha1

final_digest = SHA1(payload).hexdigest().lower()
return "media-file-hash-v1:" + final_digest
```

空文件也遵循此分支。这样所有文件都能得到同一种格式的返回值，且单次读取量最多约 8 MiB。

## 6. 实现要求

- provider 必须读取原始字节；不得解码视频、转码、解压、规范化或跳过容器字节。
- 本地 provider 可以使用 `seek + read`；远端 provider 可以使用等价的 Range 读取。读取方式
  不属于宿主协议，结果必须相同。
- 每个规定区间都必须读取足量字节；短读、对象不存在、长度变化或临时 I/O 失败均不得生成
  部分 hash。
- 文件名、路径、媒体 ID、修改时间和 provider 配置不参与计算。
- provider 可以自行缓存结果，但缓存失效和文件变更检测由 provider 负责，不形成宿主协议字段。

## 7. 固定测试向量

用于验证大文件算法的测试文件定义如下：

```text
size = 8 * MiB
file[i] = ((((1_103_515_245 * i + 12_345) mod 2^32) >> 24) & 0xff)
```

预期结果：

| 项目 | 值 |
| --- | --- |
| `slot_count` | `2` |
| `slot_1` / `middle_1_offset` | `0` / `3145728` |
| `slot_2` / `middle_2_offset` | `1` / `4194304` |
| `head_sha1` | `a8122f4890d13a2c6db13ed975dbca734b1b5424` |
| `tail_sha1` | `7b8d190ddf33b282b4b766b60d3b3ea333610b5e` |
| `middle_1_sha1` | `c92bbb8fb3fc0d7471d9ddc943a00c1da09eb846` |
| `middle_2_sha1` | `0d02b35d78e1a938b1e7ab69ebae1cd0ac34ebd5` |
| 最终 hash | `media-file-hash-v1:52385d3512a8a9ff8b6e6c5aa315e46633b28d9a` |

空文件预期结果：

```text
media-file-hash-v1:524935ebf533f3b952f2397f80691a87a7b289c7
```

## 8. 已落地内容

本约定当前已按以下范围落地：

1. 已将 `compute_file_hash` 加入 `StorageProvider` 和必需方法校验列表。
2. 已为 provider 协议补充上述边界条件和固定测试向量。
3. local provider 已按本文实现文件读取和 hash 计算。
4. `Media` 增加可空的 `file_hash` 字段及普通索引；导入创建媒体时，宿主计算并保存 provider 返回的 hash。

当前不要求新增 API、读取或按 `file_hash` 去重；已有 `Media` 不做 hash 回填，也不要求宿主读取 provider 的文件路径。
