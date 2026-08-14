# 番茄小说子插件来源与边界

`yawn_fanqie` 只请求 `https://fanqienovel.com` 的公开书籍页、阅读页和页面
返回的 HTTPS 字体资源。它不登录、不提交验证码、不尝试解锁付费章节，也不接入
第三方 API 服务。章节正文只在 `nonebot-plugin-localstore` 数据目录短期落盘，
任务数据库只保存元数据、状态和临时文件路径。

实现中的公开页面解析思路参考了：

- [fanqiexiaoshuo-Download 页面解析参考](https://github.com/zhoulianglen/fanqiexiaoshuo-Download)
- [其公开字体映射实现](https://raw.githubusercontent.com/zhoulianglen/fanqiexiaoshuo-Download/refs/heads/master/download.py)
- [fanqienovel-downloader 阅读页 DOM 解析参考](https://github.com/ying-ck/fanqienovel-downloader/blob/master/src/main.py)

本项目没有整体引入上述项目，也没有复制其下载器、任务或网络代码。provider 先读取
阅读页状态中的正文，缺失时再读取页面实际返回的
`muye-reader-content noselect` 段落；目录中的 `isChapterLock` 只作为诊断信息，
不再提前阻断正文请求。字体 glyph 映射仅作为 provider 的数据常量使用；页面没有返回
正文或字体格式异常时，章节会标记为不可用并保留可重试状态。项目不登录、不提交验证码、
不调用需要授权的接口，也不尝试伪造或解锁服务器未返回的正文。
使用者仍需自行确认目标内容的版权、平台条款和所在地区法律要求。
