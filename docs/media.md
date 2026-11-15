# MediaStore 与文件/数据库一致性

## 不可变文件与内容寻址

[MediaStore](<../app/storage/media_store.py>) 只处理文件，不依赖 PersonService、Repository、Session 或 QWidget。[MediaService](<../app/services/media.py>) 负责 Workspace、DB事务、引用和补偿。

```text
<data_dir>/media/
  .staging/<uuid>
  .locks/<workspace_uuid>/<sha256>.lock
  <workspace_uuid>/<sha256前两位>/<sha256>
```

数据库保存相对 POSIX 路径。不同 Workspace 使用不同物理路径；同 Workspace 内容相同只复用一条 Media。原图不压缩、不重编码、不覆盖，UI缩放只改显示。

导入限制：原文件最多128MiB、非空、常规文件。图片按实际解码格式决定MIME，用Pillow verify后重新读取全部帧；累计像素上限5000万，拒绝解压炸弹、伪装扩展名或损坏图像。普通非图像可保存为application/octet-stream，但不能作为头像/Gallery。未提供人脸识别、OCR、AI编辑或自动标签。

## 正常流程

1. 将原文件流式复制至同卷 staging；计算SHA256并fsync。
2. 校验暂存副本、MIME、尺寸和完整解码；不触碰原文件。
3. 获取内容级线程锁 + OS进程锁。
4. 用同卷硬链接原子发布到最终路径，目标存在则验证hash/大小并复用，绝不覆盖。
5. Service在BEGIN IMMEDIATE中重新校验Workspace/Person，按workspace/hash查找Media。
6. 创建或复用Media，已软删除Media重新激活同id；同事务建立头像/Gallery引用。新建人物＋头像的DB部分在同一事务。
7. 提交后清理staging，释放锁。

发布依赖支持硬链接的本地文件系统（本机Windows环境已验证）。不支持时明确失败，不退化为覆盖或非原子拷贝。网络盘/对象存储未支持；未来可替换文件适配器，不改变Domain。

## 内容锁与竞争

[文件锁](<../app/storage/file_lock.py>) 同时协调线程和进程。Windows使用msvcrt，Unix使用flock；默认最多等待10秒。锁文件持久保留，**不删除锁文件**，避免竞争者锁住不同inode。进程退出/崩溃后OS释放锁。

Windows锁文件首次创建必须在成功取得锁后初始化字节；锁外写字节会在其他进程已锁定该范围时失败，已通过重复多进程测试覆盖此竞争。

同内容锁覆盖发布、DB提交和补偿；写事务在读去重记录前预留写锁。UNIQUE(workspace_id,sha256)仍是最终保障，真实唯一冲突会在全事务回滚后用新Session读取并有限重试，而不是吞掉约束错误。

## 失败与保守补偿

DB和文件系统不是共同ACID事务。发布receipt记录是否本次新建；仅凭该标志不能删除。

提交/业务失败后，在仍持内容锁时使用**新的Session**检查：

- 已有Media（包括软删除记录）是否存在；
- 是否有有效引用；
- 本次是否创建该文件。

只有本次创建且两个检查明确为false时，才验证文件hash/大小并删最终文件。已有文件、已有任何Media记录或有效引用都保留。数据库状态不可确认时保留文件并记录技术错误，不冒险误删。包含“事务成功但随后抛错”的模拟测试，证明不依赖原事务内存状态。

暂存失败会清理临时文件。清理无法完成会记录异常类型，不泄露私人路径；已经提交的业务不会因为staging清理失败而被谎报回滚。强制终止/掉电可能遗留staging或孤儿文件；自动恢复/完整垃圾回收尚未实现。

## 删除与备份

PersonMedia解绑物理删除关联记录，不删除Media文件。Person归档使用deleted_at并在同事务解绑图片。Media有有效引用时禁止软删除；无引用时只设置deleted_at，原文件保留。再次导入相同workspace/hash优先重新激活原Media。

delete_if_unreferenced仅用于失败导入补偿，不是UI的删除功能；proof未知时不删。

**当前 backup 尚未实现。** 未来一致备份必须涵盖SQLite/WAL状态与完整media文件集，不能把空backups目录、单独复制主库或复制media树称为备份。

## 路径与验证边界

只接受严格canonical `media/<uuid>/<prefix>/<hash>`，拒绝绝对路径、..、反斜杠、冒号、staging/locks作为媒体路径；拒绝链接/Windows junction组件。verify使用实际SHA256。应用数据目录应由应用拥有；这些检查不构成针对同一OS用户在检查之后恶意重命名目录的安全沙箱。

符号链接拒绝测试在没有创建链接权限的Windows上明确skip；本机5项因此未执行。没有申请管理员权限或修改用户文件权限来让它们“通过”。实际断电、磁盘满、网络文件系统、跨OS、完整恢复和垃圾回收仍未验证/实现。
