# 大邮数学集自动同步

目标仓库是 `byrdocs/byrdocs-archive`。每日读取正式版 RSS，按源 metadata 的物理页码全量拆卷，从本地单卷生成封面，上传 PDF、封面和原文件备份后创建数据 PR；合并后使用原版发布程序发布当前仓库的全部元信息，再确认公开服务并清理旧对象。

## 配置

复用 Archive 正式发布工作流已经使用的 Secrets，值由仓库或组织配置提供：

| 用途 | Secrets |
|---|---|
| 正式 R2 | `R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`、`R2_FILE_BUCKET`、`R2_DATA_BUCKET` |
| 独立备份 | `BACKUP_ENDPOINT`、`BACKUP_ENDPOINT_REGION`、`BACKUP_ACCESS_KEY_ID`、`BACKUP_SECRET_ACCESS_KEY`、`BACKUP_FILE_BUCKET` |
| 正式发布接口 | `BYRDOCS_SITE_URL`、`BYRDOCS_SITE_TOKEN`、`FILELIST_SITE_URL` |

备份凭据、endpoint 和 region 单独使用。`BACKUP_ENDPOINT_REGION` 必须明确配置；R2 备份填 `auto`，其他 S3 服务按其实际 region 填写。

自动数据 PR 使用 Fine-grained Personal Access Token，存为 Secret `BUMP_PAT`：

- 仓库范围仅 `byrdocs/byrdocs-archive`；权限为 Contents、Pull requests 读写。
- 工作流在上传完成后才使用它推送同步分支和创建 PR，使已有 PR 检查可以正常触发。
- 令牌绑定个人账号且有有效期，到期前需重新签发并更新 Secret；签发人离开组织后需改用 GitHub App。

Variable `BUMP_SYNC_ENABLED=true` 开启每日／手动同步及独立清理；缺失或为 `false` 时这两个任务不执行。继续使用现有 `SITEMAP_MIN_LASTMOD`，未配置时遵循原版发布程序的行为。

## 工作流

`bump-sync.yml` 的 **BUMP sync** 提供：

- 检查 RSS 正式版，确认需要更新后才下载源文件、重切、本地生成封面、校验并上传，提供 artifact 后创建上游数据 PR。
- `version`：可指定正式版号；留空从 RSS 选择最高正式版。
- `retry`：允许重试已关闭或已合并的同版本；有待审同步 PR 时仍等待，不允许降级。

每日时间为 `17 21 * * *`：UTC 21:17，即北京时间次日 05:17。手动云端操作仅允许选择 `master`。

`upload-metadata.yml` 的 **Upload metadata** 保留默认分支推送触发，并增加手动重试入口。它沿用原来的工作目录、环境变量和触发提交，直接执行原版 `./check-format`、`./upload-metadata`，发布完整 `metadata/`，包括普通资料、教材和非大邮试卷。它与同步、清理共用 `byrdocs-storage` 并发组，并用 `queue: max` 排队，防止上传和删除交错。

每次成功发布都额外保存 `metadata.json` 和 `sitemap.xml` 到 `bump-publication-<run_id>-<run_attempt>` artifact，保留 90 天，供独立任务核对。artifact 步骤使用 `continue-on-error: true`：保存失败不会让正常发布变成失败，但会阻止对应批次清理。

`bump-cleanup.yml` 的 **BUMP cleanup** 在默认分支的定时／手动同步成功或全库发布成功后独立执行，也提供手动入口。失败上传留下的对象保留到后续成功运行或手动清理时，按原有引用保护和保留期回收。它确认公开文件与搜索更新，再检查引用、清理旧对象。

## 录入与对象布局

- 仅从明确的标题、课程标签和日期生成现有 YAML。选填字段缺失就省略；必填字段或分类无法确定则跳过，只在数据 PR 正文列出原标题、物理页码、原始标签及原因。
- 单年保留相同起止年份。明确属于试卷但缺少期中／期末信息时省略 `stage`；阶段冲突时跳过。只有明确的答案标题才标记包含答案。排除高考、竞赛、外校及作业条目。
- 仅收录学年起始年份不早于 2016 年的试卷；跳过原因列入 PR 正文。
- 学院按标题中明确出现的名称录入。长名称优先匹配，别名转换后去重保留；未识别到就省略 `college`。学院列表是本地固定表，新增名称时需同步更新。
- 每版全量重切，在每份单卷末尾附上从原书切出的版权声明与授权许可页，现有 PDF 属性和附件保留来源。
- 同步试卷使用 `type: test`、`data.filetype: bump` 标识来源；其余元信息按 PDF 试卷处理，文件和下载地址仍为 `<md5>.pdf`。网站据此显示链接到 `https://pub.fa-555.com` 的 bump 按钮。
- 新版成功集合替换上一版。下载失败、损坏 PDF、页码越界或重叠、零有效条目都终止替换。
- 已有非本任务管理的同 MD5 元信息保持原样；预先存在的存储对象会核对并复用，不自动取得其删除权。已有原文件与备份核对内容，允许 PDF 的通用二进制类型声明；同步阶段只检查已有封面是否可读取、格式及类型声明，不修改其尺寸或编码。
- 文件桶根目录为 `<md5>.pdf`、`<md5>.jpg`、`<md5>.webp`；备份桶根目录仅备份 `<md5>.pdf`。数据桶仍使用 `metadata.json`、`sitemap.xml`，schema 上传继续由现有工作流负责。
- `sources/bump.json` 是仓库内部清单，数据桶 `_bump/state.json` 和 `_bump/confirmed/` 是运行记录。

## 全库发布与清理

发布使用固定的 `byrdocs-check v6.5.2`（`check-format` 和 `upload-metadata`）。发布程序直接读取真实仓库中的 YAML 和 Git 修改日期，连接正式临时上传与预览接口，大邮脚本只读取发布结果。

### 封面与发布确认

封面在同步构建阶段由 `covers.py` 直接读取拆好的本地 PDF，使用 PDFium 渲染第一页，最长边 2000 像素，保持长宽比。PDFium 调用串行执行，避免其线程安全限制。依赖固定为 `pypdfium2==5.15.0`，安装包自带 PDFium。

本地封面全部生成并校验后，才记录缺失对象的归属，并上传 PDF、JPG、WebP 和原文件备份；每个对象读回校验成功后才将批次标为就绪并进入提 PR 阶段。已有有效对象核对后复用，已有封面保留尺寸、编码与删除归属。封面上传中断按账本重试。合并后的原发布程序复用已经存在的两种封面；当前批次封面仍在独立发布确认中读回检查，异常时阻止清理。

独立清理任务先查询 **Upload metadata** 在 `master` 上的最新运行。只有该运行成功、提交号等于当前默认分支且来源为正式仓库的推送或手动运行，才下载对应运行次数的 artifact。核对全库条目、sitemap、正式桶两份文件和大邮封面后，将确认副本写入 `_bump/confirmed/`，并记录提交号与运行次数。确认中断只影响清理，重跑 **BUMP cleanup** 继续。

当前提交的发布仍在排队或运行时，清理正常退出，等待发布成功事件再次触发；已结束且失败或取消时才报错。发布文件按字节和基础 media type 核对。

同一次发布已确认后，后续清理直接复用确认副本，仍检查运行状态与桶内内容，不受 artifact 后续过期影响。新运行或重试必须有各自的 artifact。原发布失败或两份文件只更新了一份时，同样先完成原发布，再重试清理。

删除前依次确认：

1. 当前提交存在成功的发布运行，桶内 `metadata.json`、`sitemap.xml` 与该次发布的输出相符。
2. 数据域名与主站公开元信息、主站 sitemap 均已更新；通过现有 `BYRDOCS_SITE_TOKEN` 鉴权下载新批次 PDF，校验 MD5。鉴权请求不跟随重定向。
3. 从本次元信息公开确认起等待至少 5 分钟，再通过现有搜索 API 检查新旧受管理 ID。等待不占用运行中的休眠任务；下一次每日同步结束后的独立清理或手动运行 **BUMP cleanup** 继续。
4. 汇总当前仓库全体元信息、已发布元信息及所有待审 PR 中 YAML 的引用；只删除内部所有权记录中的无引用对象。仓库及线上元信息严格解析；待审 PR 尚未通过校验，保守保护其元信息文件名和原始内容中所有 MD5，即使 YAML 或 URL 未写完也保留可能的引用。无法取得 PR 内容时仍停止清理。
5. 清理每份卷前再次检查默认分支和待审 PR 是否变动；有变化则停止，重试时重新收集引用。

清理的失败语义：

- 部分上传、部分发布、公开访问异常、搜索仍未更新、引用发生变化时，都不继续清理。
- 删除前先把候选写入账本；整批完成或遇到异常时统一保存进度。
- 已保存进度的失败只重试剩余对象。进程被强制终止或最后保存失败时可能重复执行已完成的删除，删除本身是幂等的。
- 任何重试都先重新收集引用、再计算候选，不能直接重放上一次的删除队列。
- 关闭未合并 PR 的文件按同样规则回收。

每次上传尝试都会先写入 `attempted_at`，即使复用同一批次或上传失败，也从本次尝试起为尚未关联新 PR 的产物保留至少 24 小时。早于本次尝试的同名分支关闭记录不取消这段保留期；本次尝试之后关闭的 PR 按正常关闭流程回收。

`_bump/state.json` 是删除归属账本。每次保存先写入并读回主账本，再将相同内容写入并读回数据桶中的 `_bump/state.backup.json`；副本保存失败会终止当前大邮任务，主账本仍是权威记录。

主账本缺失而副本存在时，任务报错并停止。若两份账本都丢失，清理因没有归属记录退出。发布快照和 Actions 运行记录只能证明发布内容，无法重建哪些对象曾由本任务创建。没有可信备份时保留归属不明的旧对象；重新同步可恢复当前批次，但不会取得已有对象的删除权。

## 本地验证与日常维护

```sh
python -m pip install -r scripts/bump/requirements.txt
python -m unittest discover -s scripts/bump -p 'test_*.py' -v
python scripts/bump/sync.py build --version <版本号> --output output/bump/review
```

输出目录必须为空；可用 `--source-pdf`、`--source-metadata` 指定已下载的同版源文件。`--apply` 只在整批构建成功后修改本地受管理 YAML 和内部清单。单卷 PDF、JPG/WebP 封面、元信息、清理候选及 `pr-body.md` 的 Actions artifact 保留 14 天。即使创建 PR 失败，仍可查看已生成的跳过明细和报告。

BUMP sync 的 test 任务先运行 Ruff 的代码与格式检查、actionlint 的工作流检查，再运行单元测试。本地可运行相同的 Python 检查：

```sh
python -m pip install ruff==0.17.4
ruff check scripts/bump
ruff format --check scripts/bump
```

运行失败时查看对应工作流：上传或 PR 创建失败重跑 **BUMP sync**；发布失败手动运行 **Upload metadata**；公开确认及删除失败重跑 **BUMP cleanup**。外部修改已发布 JSON 时应先核对，再用正式发布流程恢复，不能删除内部状态绕过保护。

暂停同步和清理：将 `BUMP_SYNC_ENABLED` 设为 `false`。

修改发布程序版本或搜索缓存策略时，应同时复验全库输出和公开确认逻辑。
