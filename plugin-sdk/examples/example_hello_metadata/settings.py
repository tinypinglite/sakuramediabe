"""插件配置模型；宿主用它的 JSON Schema 渲染插件设置页。"""

from pydantic import BaseModel, ConfigDict, Field

from .translations import text_map


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title_prefix: str = Field(
        default="[示例] ",
        title="标题前缀",
        description="拼在示例影片标题前面的文字。",
        json_schema_extra={
            "title_i18n": text_map("settings.title_prefix.title"),
            "description_i18n": text_map("settings.title_prefix.description"),
        },
    )
