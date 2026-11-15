# Phase 3 验收记录

## 最终结果

执行：

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q -rs
```

**总计431项：426通过、0失败、5跳过，退出码0。** 最终完整运行40.54秒。

5项跳过全部来自临时符号链接创建测试：当前Windows用户没有创建符号链接权限。不是功能失败，也不能宣称已经通过；没有申请管理员权限或修改系统文件权限来绕过。

| 测试组 | 用例数 |
|---|---:|
| 原Phase1/Phase2回归（保留并阶段化空schema断言） | 85 |
| 正式模型DDL/UTC类型 | 2 |
| 新迁移数据库约束/UTC重载 | 13 |
| Domain实体/校验/纯依赖 | 200 |
| SQL Repository映射与scope | 41 |
| Workspace/Person服务 | 5 |
| Media Service集成与并发/补偿 | 23 |
| MediaStore/文件锁 | 54（49通过、5条件跳过） |
| BEGIN/BEGIN IMMEDIATE | 2 |
| 真实服务驱动UI | 6 |

测试库全部通过迁移创建；事务fixture使用独立MetaData，不污染正式schema。临时生成的图片仅是测试fixture，不是应用示例图片，不向真实用户目录插入人物/媒体。

## 实测环境

Windows、conda Python3.14.6、PySide6 6.11.0、SQLAlchemy2.0.51、Alembic1.20.0、platformdirs4.9.4、Pillow12.3.0。使用局部虚拟环境和 `-s` 避免用户级Qt冲突，未升级现有依赖。

## 已验证业务

- Workspace创建/读取/修改/软归档、名称冲突和Default幂等初始化。
- Person创建/编辑/归档，名称不唯一，UUID和所有时间字段UTC-aware重载。
- 别称增删、自定义类型、按display/real/alias搜索及%/_字面转义。
- 图片校验、尺寸、SHA256、原文件不变、同Workspace去重和跨Workspace不同物理路径。
- Avatar替换唯一、Gallery多图、同Media被多个人物复用。
- Service拒绝跨Workspace引用；直接DB写入也被复合FK拒绝。
- 解绑不误删原图，Person归档解绑引用，Media有引用不可软删除。
- Media软删除保留文件；重导入重新激活相同id。
- 新建Person＋头像的DB部分整体提交或回滚。
- 业务失败和before_commit失败补偿；已有文件不误删。
- fresh proof不可读取或“实际提交成功后抛错”时保守保留文件。
- 实际UNIQUE冲突后的新Session读取/有限重试。
- 两线程及Windows spawn双进程同时导入相同内容，最终单条Media。
- 文件锁三进程竞争重复5次，互斥及原子发布复用。
- 读取BEGIN、写入BEGIN IMMEDIATE及连接池标记不泄漏，失败释放写锁。
- UI创建/编辑/Alias/Workspace切换/归档/图片/原图缩放；UI日志不输出私人错误值。

## 迁移与原生窗口验收

另一个自动清理的临时目录执行：

```text
upgrade 0001 → upgrade 0002 → downgrade 0001 → upgrade 0002
```

Alembic metadata检查无新增操作；0001只有版本表，0002恰好五业务表加版本表。

使用**正常Windows Qt平台**显示真实People页，只有Default Workspace、无生成的人物。窗口与页面可见，服务已真实装配。随后运行两次独立应用进程 smoke test，日志确认 `Qt platform=windows`，均退出0；重启版本为0002，Default仅一条，error.log为空。

[空人物界面的原生截图](<../.verification/phase3-people.png>) 已生成，供人工审阅；窗口启动和控件自动测试不等同人工视觉/DPI验收。

## 发现并修复的问题

1. 旧logger测试修改全局propagate/handler状态：加入测试级隔离，保留私人SQL参数日志断言。
2. Windows锁文件初始化字节在锁外写入：三进程竞争偶发失败，改为取得锁后初始化，并重复验证。
3. Qt Workspace下拉对Python UUID对象匹配按对象身份导致findData不稳定：下拉存canonical UUID字符串，Service边界恢复UUID。
4. Phase2测试“最新metadata/head必须为空”与正式schema冲突：保留显式0001空schema测试，head验证五表，不删除历史回归。

## 边界与下一步

- 没有Relationship/Event/Evidence/Analysis/Memory/LLM/Graph/Sync/ChangeLog或这些空表。
- 没有完整backup、崩溃恢复、孤儿GC或异步导入进度UI。
- 原子发布依赖支持硬链接的本地文件系统；不支持时明确失败，不覆盖原图。
- 符号链接5项、其他OS、实际PostgreSQL服务、网络盘、断电/磁盘满、wheel/exe分发及多DPI人工评审未完成。
- PostgreSQLDDL已编译验证，不能等同实际PostgreSQL集成测试。
- 下一阶段仅在用户授权后进入Phase4 Relationship/RelationshipHistory。

Phase2的历史85项验收仍保留在 [历史记录](<verification.md>)；当前实现以本记录与 [README](<../README.md>) 为准。
