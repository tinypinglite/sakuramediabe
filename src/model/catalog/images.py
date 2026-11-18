import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin


class Image(TimestampedMixin, BaseModel):
    origin = peewee.CharField(unique=True, max_length=255, help_text="原图路径")

    class Meta:
        table_name = "image"


# 影片资产按目录前缀查询 origin（LIKE '目录/%'）；默认排序规则下该模式用不上 origin
# 唯一索引，text_pattern_ops 按字节序比较，保证前缀匹配走索引扫描而不是全表扫。
Image.add_index(
    peewee.ModelIndex(
        Image,
        ("origin text_pattern_ops",),
        name="image_origin_pattern",
    )
)
