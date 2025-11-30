# 贡献指南

欢迎你向本项目贡献文件！本文档将会简要介绍如何为 BYR Docs 添加文件及修改信息。

我们为你提供了 [BYR Docs Publish](https://publish.byrdocs.org)。这是一个简单易用的 BYR Docs 文件管理器，可以帮助你高效完成 BYR Docs 的信息编辑工作。

## 准备工作

1. 首先你需要拥有一个 [GitHub](https://github.com) 账号；
2. [Fork 本仓库](https://github.com/byrdocs/byrdocs-archive/fork)到自己的 GitHub 账号下；
3. 通过 GitHub 账号[登录 BYR Docs Publish](https://publish.byrdocs.org/login)；
4. 登录后[绑定你 Fork 的仓库](https://publish.byrdocs.org/bind)。

以上工作完成后，你将能在 BYR Docs Publish 首页的右上角看到自己的 GitHub 账户名和已绑定的仓库。如果该信息不正确，请对照 [https://publish.byrdocs.org/bind]() 进行检查。如果你确信这是网站错误造成的，欢迎你向我们报告错误。

## 添加文件

首先，你需要确保你上传的文件符合本站的 [文件收录规则](docs/文件规则.md#文件收录规则)。

在 BYR Docs Publish 首页中选择「添加文件」即可开始新文件上传工作。

1. 根据[文件收录规则](docs/文件规则.md#文件收录规则)，选择文件类型；
2. 选择本地文件并上传；
3. 填写有关试卷的详细信息；
4. 预览暂存，你可看到 yaml 格式的结果；
5. 如果还有其它需要上传的文件，可以继续添加。
6. 全部添加完毕后回到首页，能够看到你已经暂存的所有元信息，你可继续编辑和管理。一切就绪后，提交你的更改。
7. 你的更改会在 [byrdocs/byrdocs-archive](https://github.com/byrdocs/byrdocs-archive) 中以 Pull Request 的形式提出，等待管理员处理。

我们暂时没有设计修改 pull request 的功能。如果在 pull request 审核期间需要做新的更改，请直接对 yaml 文件进行修改。如需更多信息，请参阅[关于元信息](docs/关于元信息.md)。

## 修改文件

在 BYR Docs Publish 首页中选择「修改文件」即可开始文件编辑工作。每当你暂存编辑结果后，都可以在 [编辑文件列表](https://publish.byrdocs.org/edit) 中看到修改记录。文件编辑包括以下三种情况：

- **对元信息进行的编辑**，直接修改元信息即可；
- **对文件进行的改动**，BYR Docs 不支持直接编辑文件，所以请在本地完成编辑，然后重新上传改动后的文件。该操作会删除原文件，并创建新文件，再将元信息迁移到新文件下，所以你将看到修改记录中出现一次「删除」和一次「新增」；
- **删除一份文件**，会将该文件对应的元信息删除。

你可以随时撤销某条更改，或在此基础上继续编辑。

## 补充说明

- BYR Docs 为用户提供了丰富的快捷键，以便提高录入信息的效率。
