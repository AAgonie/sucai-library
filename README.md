# 短剧广告大图素材库展示网站

面向内网使用的图片素材浏览、检索与审查网站。项目提供本地或局域网部署方式，图片文件留在已授权的数据源中，服务端仅按配置读取和管理其索引记录。

> 公开仓库不包含任何运行配置、账号、密码、素材文件、数据库、日志、内网地址或实际共享路径。请使用 `config.example.json` 创建本机配置。

![已脱敏的界面预览](docs/assets/interface-preview-redacted.png)

## 功能

- 通过 Windows SMB（Server Message Block，Windows 文件共享协议）路径或 HTTP 数据源扫描已授权图片。
- 按比例、日期、文件夹、作者、渠道、标签与 OCR（Optical Character Recognition，光学字符识别）文字筛选。
- 原图、16:9、1:1 与 9:16 派生版本按实际比例展示，并支持在详情中切换查看。
- 使用浏览器分别下载所选文件；不创建压缩包，也不需要浏览器证书配置。
- 支持批量 OCR、OCR 无效词、重复审查、隐藏/恢复记录与操作进度。
- OCR 识别到书名号内容时会自动隐藏该图片，并在界面报告原因。
- 内置单进程 OCR 串行保护、健康检查与启动守护，避免长任务阻塞其他网页功能或因服务异常长期离线。

## 快速开始

1. 安装 Python 3.12。
2. 在项目根目录复制 `config.example.json` 为 `config.local.json`，并填写自己的数据源信息；不要提交这个文件。
3. 安装基础依赖：

   ```powershell
   python -m pip install -r backend\requirements.txt
   ```

4. 需要 OCR 时，按 [本地 OCR 说明](docs/ocr-local.md) 安装额外依赖。
5. 本机运行 `start.cmd`；局域网服务机运行 `start-nas.cmd`。
6. 打开 `http://127.0.0.1:8000`，先进行连接测试，再扫描入库。

从零部署、OCR 安装、局域网访问、自动重启与故障排查见[完整部署、OCR 与故障排查手册](docs/部署与故障排查.md)。

## 配置原则

`config.example.json` 中的 `FILE_SERVER` 和 `shared\\image-library` 只是占位符，必须替换为部署环境实际可访问的数据源。账号密码仅保存在本机 `config.local.json`，公开仓库不会读取或提供任何真实凭据。

## 开发与验证

前端位于 `frontend`，构建命令：

```powershell
Set-Location frontend
npm ci
npm run build
```

后端测试：

```powershell
python -m unittest discover -s backend\tests
```

## 发布边界

请勿把下列内容提交到仓库：运行配置、数据库、日志、导出的素材、网络共享路径、设备名、内网地址、账号、密码、客户名称或任意未经授权的素材文本。`.gitignore` 已覆盖常见本地运行文件；发布前仍应执行一次敏感信息检查。

## 许可与使用

此项目用于获授权的内部素材管理场景。部署者应自行确认图片、文本及数据源的访问授权，并遵守所在组织的数据安全要求。
