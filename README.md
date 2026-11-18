# tavern-stream

文本协议编排、流式连接、取消与滚动发布。

本仓库采用「固定公开上游 + 已审程序覆盖」：UPSTREAM 锁定精确 Git 提交，overlay/ 是需要覆盖的程序文件。materialize.sh 在新目录取回该提交并组装；不移动原 AetherStream 仓库的任何分支。没有发布生产 runtime-flags、账户、日志、私人提示词语料或蓝绿发布记录。

本地文字注入样例改为空值，按需在私有部署环境设置 TAVERN_PRO_OPUS…；没有上传原提示词正文。
