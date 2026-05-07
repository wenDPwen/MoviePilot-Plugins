# MoviePilot 自用插件仓库

这是一个 MoviePilot V2 第三方插件仓库，仓库结构保持为 MoviePilot 插件市场可读取的格式。

## 插件

- RSS资源择优下载
- 院线电影订阅

## MoviePilot 使用方式

在 MoviePilot 中添加第三方插件市场仓库时，填写本仓库 GitHub 地址，例如：

```text
https://github.com/<owner>/<repo>
```

MoviePilot 会读取根目录的 `package.v2.json`，并从 `plugins.v2/` 下安装对应插件。
