"""慧湖通门户自动认证守护（Python v5）。

仅依赖 Python 3.8+ 标准库：
- OpenID 是唯一凭证，satoken 由 certificateLogin 自动换发并缓存；
- 断网事件结构化记录（JSONL）+ 实时状态文件；
- 易断网时间窗内自动提高探测频率，离线时指数退避快速重连。
"""

__version__ = "5.0"
