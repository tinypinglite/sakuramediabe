"""进程级运行标识：每次进程启动生成一次，用于前端区分重启前后的后端实例。"""

import uuid

BOOT_ID = uuid.uuid4().hex
