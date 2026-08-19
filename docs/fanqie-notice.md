# 番茄小说子插件来源与边界

`yawn_fanqie` 默认请求 `https://fanqienovel.com` 的公开书籍页、阅读页和页面返回的
HTTPS 字体资源；书籍搜索会用 Playwright Chromium 打开公开搜索页，让页面自身运行
官方安全 SDK 并发起搜索请求。当阅读页明确标记章节免费但只给预览时，还会请求配置的
固定 App 协议，再按顺序回退到第三方 `/api/raw_full`、fanqietc 和管理员本机 helper。
它不登录、不提交验证码、不请求付费章节。章节正文
只在 `nonebot-plugin-localstore` 数据目录短期落盘，任务数据库只保存元数据、状态和
临时文件路径。

书籍搜索只监听公开搜索页实际发出的官方搜索响应，榜单读取番茄公开榜单页及其服务端
数据；搜索结果按书名/作者由平台执行模糊匹配，榜单结果不写入数据库。默认使用
localstore 下的插件专用持久化 BrowserContext，让页面生成的 session/fingerprint cookie
在后续搜索中继续可用；不读取或复用用户已有浏览器配置文件、QQ 登录态、手工令牌或
安全参数。首次空响应会在同一会话内重试一次。接口仍要求验证码、持续返回空响应或
结构变化时，插件只报告暂时不可用，不自动提交验证码或绕过访问控制。

搜索接口的 `book_name`、`author` 等字段可能使用 PUA 字符选择性混淆。浏览器适配器会
在同一页面响应之外保留页面运行时注入的字体样式，provider 对当前字体的 glyph 轮廓做
稳定哈希，再匹配插件内的公开字符表。映射缺失时不会把混淆字符当作正常书名继续进入
状态机。长简介只做已知字符的尽力解码，搜索交互不展示该字段。

搜索运行时由项目依赖提供，首次部署需要执行 `uv run playwright install chromium`。
`FANQIE_BROWSER_TIMEOUT` 控制页面和响应等待时间，`FANQIE_BROWSER_HEADLESS` 控制是否
以 headless 模式启动，`FANQIE_BROWSER_PROFILE_DIR` 可指定一个专用会话目录（留空时由
localstore 提供）。不要把它指向正在运行的个人 Chrome/Edge 配置文件。浏览器运行时缺失
时不会阻断插件发现。浏览器保持 Playwright 标准行为，不修改自动化标记，不伪造账号、
Cookie、指纹令牌或验证结果；搜索仍要求官方页面自行生成 session/fingerprint 上下文。书籍链接、榜单和章节的
既有公开 HTTP 流程不受影响。

实现中的公开页面解析思路参考了：

- [fanqiexiaoshuo-Download 页面解析参考](https://github.com/zhoulianglen/fanqiexiaoshuo-Download)
- [其公开字体映射实现](https://raw.githubusercontent.com/zhoulianglen/fanqiexiaoshuo-Download/refs/heads/master/download.py)
- [fanqienovel-downloader 阅读页 DOM 解析参考](https://github.com/ying-ck/fanqienovel-downloader/blob/master/src/main.py)
- [Tomato Novel Downloader 本机客户端 helper](https://github.com/zhongbai2333/Tomato-Novel-Downloader)
- [mcp-server-fanqie 第三方 API 封装](https://github.com/fysh1010/mcp-server-fanqie)
- [fanqie-rs Reading 7.2.1 App 协议参考（固定提交）](https://github.com/ZreXoc/fanqie-rs/tree/906c6fd5744af0ef49e529102cdb64a250c067f7)

第三方正文服务默认地址为 `http://101.35.133.34:5000`，可通过
`FANQIE_THIRD_PARTY_API_BASE` 覆盖。该节点失败时，插件会切换到公开前端
`https://api.fanqietc.com/proxy?api=default&action=content&item_id=...`，其地址和公开
前端 token 可分别由 `FANQIE_THIRD_PARTY_FALLBACK_BASE`、
`FANQIE_THIRD_PARTY_FALLBACK_TOKEN` 覆盖。raw_full 响应校验标题、全部免费段落及章节字数；
回退代理没有标题字段时，插件会校验官网 Reader 预览前缀、章节字数和正文完整性。请求不
携带 QQ、Cookie、账号或密码；前端 token 不是用户登录凭据，服务端轮换后应更新配置。
这是外部服务，不保证长期可用；将主地址置空会关闭两个第三方镜像，但不影响由
`FANQIE_APP_PROTOCOL_ENABLED` 独立控制的官方 App 直连和自备 helper。

进程内 App 协议固定复现 `ZreXoc/fanqie-rs` 提交
`906c6fd5744af0ef49e529102cdb64a250c067f7`。该提交的 `Cargo.toml` 声明 MIT，提交树没有
单独的 `LICENSE` 文件；本项目记录来源与完整版本，不宣称上游包含额外许可文本。固定画像
为 Reading 7.2.1.32 / iPad，内置其抓包 `device_id`、`iid`、`cdid`、`x-tt-dt` 等样本，
并复现 `X-Gorgon`、`X-Khronos`、`X-Ladon`、`X-Argus`、`X-Helios`、`X-Medusa` 六签名、
`registerkey`、单章 `batch_full`、AES-CBC/PKCS#7 和 gzip 解码。

上游固定提交没有网络 `device_register` 端点，而是直接使用抓包匿名画像；本插件的“匿名
画像注册”同样指把该固定版本画像原子写入 localstore，而不是生成或伪造新的服务端设备。
JSON schema 或画像版本失效、损坏，或服务端明确拒绝该设备时，只允许清除并重新初始化
一次。协议仅允许 `https://api5-normal-sinfonlinea.fqnovel.com` 及固定业务路径，不跟随
重定向；设备 ID、完整签名 URL、请求头和正文密钥不进入日志。正文 AES key、keyBlob 和
key version 只在进程内存中存在；章节 key version 变化时最多刷新一次 `registerkey` 并
重取正文。可通过 `FANQIE_APP_PROTOCOL_ENABLED=false` 禁用 App 来源。

本项目没有整体引入上述项目；App 模块只按固定提交移植所需签名、请求和解密原语，并在
代码与文档中保留来源版本。provider 先读取
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
章节。远程源的 5xx、超时和网络错误会保留为可重试失败，不会合并缺章成品；远程源返回
可确定的无效正文且本机 helper 也不可用时，章节才会标记为不可用，而不是把预览误作全文。
页面没有返回正文或字体格式异常时，章节会保留可重试状态。项目不登录、不提交验证码、
不调用登录接口，也不生成付费访问凭据。
使用者仍需自行确认目标内容的版权、平台条款和所在地区法律要求。
