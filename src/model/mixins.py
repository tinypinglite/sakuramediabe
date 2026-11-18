from peewee import DateTimeField

from src.common.runtime_time import utc_now_for_db
from src.model.base import BaseModel


class TimestampedMixin(BaseModel):
    created_at = DateTimeField(default=utc_now_for_db)
    updated_at = DateTimeField(default=utc_now_for_db)

    def save(self, *args, **kwargs):
        """更新已有行时自动推进 updated_at。

        peewee 的 ``default=`` 只在 INSERT 期生效、不会在 save() 时重算，所以在没有这段覆写之前
        updated_at 的实际语义是"创建时刻"——任何按它排序的"最近修改优先"列表（playlist、
        clip_collection、media、background_task_run、download_task 等）排的其实是创建顺序，而且
        不会报任何错。此前各 service 靠手工 ``obj.updated_at = now`` 逐处补，谁忘了谁静默失效。

        ``save(only=[...])`` 也要把本列带上，否则时间戳只在内存里推进、落不了库。

        本覆写只对 ``save()`` 生效：``Model.update(...)`` / ``insert_many(...)`` 这类批量写
        完全绕过实例方法，仍需调用方自己带上 updated_at。
        """
        if self._pk is not None:
            self.updated_at = utc_now_for_db()
            only = kwargs.get("only")
            if only is not None:
                kwargs["only"] = self._with_updated_at(only)
        return super().save(*args, **kwargs)

    @classmethod
    def _with_updated_at(cls, only) -> list:
        """把 updated_at 补进 ``only`` 列表（已有则原样返回）。

        判定必须按「字段名」而不是 ``in``：peewee 的 ``Field.__eq__`` 被重载成返回 Expression，
        而 Expression 恒为真值，所以 ``field in [...]`` 对任何非空列表都返回 True——写成 `in`
        会让这段补列逻辑变成永不执行的死代码（updated_at 静默落不了库）。
        peewee 的 ``only`` 同时接受 Field 对象和字段名字符串，两种都要认。
        """
        only_fields = list(only)
        field_name = cls.updated_at.name
        for field in only_fields:
            name = field if isinstance(field, str) else getattr(field, "name", None)
            if name == field_name:
                return only_fields
        only_fields.append(cls.updated_at)
        return only_fields
