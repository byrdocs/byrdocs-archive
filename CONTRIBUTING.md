# 贡献指南

欢迎你向本项目贡献文件！在此之前请你仔细阅读本方针的内容，通常情况下你应当遵守本方针。

## 前提

首先，你需要确保你试图上传的文件符合本站的 [文件收录规则](https://github.com/byrdocs/byrdocs-archive/wiki/%E5%85%B3%E4%BA%8E%E6%96%87%E4%BB%B6#%E6%96%87%E4%BB%B6%E6%94%B6%E5%BD%95%E8%A7%84%E5%88%99)。

## 步骤

1. [Fork 本项目](https://github.com/byrdocs/byrdocs-archive/fork)。
2. 安装 [byrdocs-cli](https://github.com/byrdocs/byrdocs-cli)。
    ```bash
    pip3 install byrdocs-cli
    ```
    或者
    ```bash
    pipx install byrdocs-cli
    ```
3. 登录。
    ```bash
    byrdocs login
    ```
4. 上传文件。
    ```bash
    byrdocs upload /path/to/file
    ```
    例如：
    ```bash
    $ byrdocs upload 2020期中.pdf
    Uploading: 100%|█████████████████████████████████████████| 716k/716k [00:05<00:00, 142kB/s]
    File uploaded successfully
        File URL: https://byrdocs.org/files/0a2ec7a3b027bb439e7bf021c4b1b1be.pdf
    ```
5. 在 metadata 中新建一个名为 `<md5>.yml` 的文件，填写文件的元信息，其中 `<md5>` 是你上传的文件的 md5 值。在上一步的例子中，它是 `0a2ec7a3b027bb439e7bf021c4b1b1be`，所以应当创建 `metadata/0a2ec7a3b027bb439e7bf021c4b1b1be.yml` 文件。
6. 编写文件元信息，参见 [文件元信息](https://github.com/byrdocs/byrdocs-archive/wiki/%E5%85%B3%E4%BA%8E%E6%96%87%E4%BB%B6#%E6%96%87%E4%BB%B6%E5%85%83%E4%BF%A1%E6%81%AF)。
7. 提交并推送到你 Fork 的仓库，创建一个 Pull Request。
8. 维护者审核、合并。

如果在上传文件时遇到如下问题：

```shell
$ byrdocs upload 2020期中.pdf
Error from server: 文件已存在
```

说明此文件已在服务器存在，如果你确信在 [byrdocs.org](https://byrdocs.org/) 上无法搜索到相关内容，该文件可能正在由其他人上传、编写元信息和提交 Pull Request。你可以考虑暂缓添加此文件。

## 其他修改方式

1. **修改文件内容**：使用 `byrdocs-cli` 上传新文件，并更新对应 `yml` 文件的文件名、`id` 和 `url` 字段；
2. **修改文件元信息**：直接修改对应的 `yml` 文件；
3. **删除文件**：直接删除对应的 `yml` 文件即可。

## 另请参见

- [关于文件](https://github.com/byrdocs/byrdocs-archive/wiki/%E5%85%B3%E4%BA%8E%E6%96%87%E4%BB%B6)
- [如何整理试题文件？](https://blog.byrdocs.org/blog/posts/how-to-organize-test/post.html) - 一个更详细的试题整理教程
