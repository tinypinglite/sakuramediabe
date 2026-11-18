from src.schema.common.base import SchemaModel


class ClipCollectionSummary(SchemaModel):
    """片段所属合集的轻量摘要，供片段详情回显「该片段已加入哪些合集」。

    对称影片详情的 ``PlaylistSummaryResource``，前端「加入合集」选择器据此勾选已加入项。
    """

    id: int
    name: str
