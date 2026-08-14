# 番茄小说子插件来源与边界

`yawn_fanqie` 默认请求 `https://fanqienovel.com` 的公开书籍页、阅读页和页面返回的
HTTPS 字体资源；当阅读页明确标记章节免费但只给预览时，还会请求配置的第三方
`/api/raw_full` 正文接口。它不登录、不提交验证码、不请求付费章节。章节正文只在
`nonebot-plugin-localstore` 数据目录短期落盘，
任务数据库只保存元数据、状态和临时文件路径。

实现中的公开页面解析思路参考了：

- [fanqiexiaoshuo-Download 页面解析参考](https://github.com/zhoulianglen/fanqiexiaoshuo-Download)
- [其公开字体映射实现](https://raw.githubusercontent.com/zhoulianglen/fanqiexiaoshuo-Download/refs/heads/master/download.py)
- [fanqienovel-downloader 阅读页 DOM 解析参考](https://github.com/ying-ck/fanqienovel-downloader/blob/master/src/main.py)
- [Tomato Novel Downloader 本机客户端 helper](https://github.com/zhongbai2333/Tomato-Novel-Downloader)
- [mcp-server-fanqie 第三方 API 封装](https://github.com/fysh1010/mcp-server-fanqie)

第三方正文服务默认地址为 `http://101.35.133.34:5000`，可通过
`FANQIE_THIRD_PARTY_API_BASE` 覆盖。插件只调用其 `GET /api/raw_full?item_id=...`
接口，并校验返回标题、全部免费段落及章节字数；请求不携带 QQ、Cookie、账号或密码。
这是外部服务，不保证长期可用，管理员可将配置置空后改用自备 helper 回退。

本项目没有整体引入上述项目，也没有复制其下载器、任务或网络代码。provider 先读取
阅读页状态中的正文，缺失时再读取页面实际返回的
`muye-reader-content noselect` 段落；目录中的 `isChapterLock` 只作为诊断信息，
不再提前阻断正文请求。字体 glyph 映射仅作为 provider 的数据常量使用。

如果阅读页同时明确标记 `needPay=0`、`isPaidPublication=false`、
`isPaidStory=false`，且 `chapterWordNumber` 显示页面只返回明显过短的预览，可由管理员
显式设置 `FANQIE_MOBILE_HELPER_PATH`。插件会为该单章启动用户自行安装的兼容 helper：
只绑定临时的 `127.0.0.1` 端口、使用临时数据与导出目录、强制其官方客户端模式和单章
TXT 导出，完成后立即关闭并删除临时目录。插件不会传递 QQ、Cookie、登录态或密码，也
不会自动下载或更新 helper；导出的章节标题必须与网页标题相同才会被接受。

第三方接口和 helper 都不会用于 `needPay` 非零、任一付费标记为真或阅读页缺少免费标记的
章节。两者均不可用时，明确为免费但只返回预览的章节会标记为不可用，而不是把预览误作全文。
页面没有返回正文或字体格式异常时，章节会保留可重试状态。项目不登录、不提交验证码、
不调用需要授权的接口，也不伪造签名、令牌或付费访问凭据。
使用者仍需自行确认目标内容的版权、平台条款和所在地区法律要求。
