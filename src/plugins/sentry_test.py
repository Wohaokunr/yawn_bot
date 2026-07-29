from nonebot import on_command

sentry_test = on_command("sentry_test", priority=1, block=True)


@sentry_test.handle()
async def handle_sentry_test() -> None:
    raise RuntimeError("YawnBot Sentry 测试异常")