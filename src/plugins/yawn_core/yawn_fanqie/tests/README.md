番茄 provider 的回归测试位于仓库根目录 `tests/test_fanqie_regressions.py`，因为
NoneBot 插件包必须在 `nonebot.init()` 后正式发现，避免直接导入子目录测试时绕过
正式插件生命周期。
