# Phase 2 验证记录

## 实测环境

- Windows，本地 conda Python 3.14.6。
- PySide6 6.11.0、SQLAlchemy 2.0.51、Alembic 1.20.0、platformdirs 4.9.4。
- 项目局部 .venv 复用 conda 依赖，仅新增缺失的 Alembic/Mako；运行使用 `-s`，避免用户级 Qt 与 conda Qt 冲突。
- 未升级 PySide6。SQLAlchemy 最低 2.0.22 对应永久关闭 Session API，Alembic 最低 1.16 对应 path_separator 配置。

## 最终全量测试

```powershell
& .\.venv\Scripts\python.exe -s -m pytest -q
```

**总计 85 项：通过 85，失败 0，跳过 0。** 最终一次运行耗时 12.45 秒。原 Phase 1 的 16 项测试全部保持通过。

| 测试组 | 数量 | 主要验证 |
|---|---:|---|
| Phase 1 | 16 | 五页 UI、原配置、日志 |
| 新配置与路径 | 19 | platformdirs、TOML 基准、绝对路径、非法配置 |
| 数据库/Session/事务/线程 | 32 | 连接 PRAGMA、原子提交、整体回滚、生命周期、线程隔离、只读错误、锁超时、WAL 快照 |
| Alembic 迁移 | 12 | 空库升级/降级/重升、版本、重启、未知库拒绝、不同 cwd CLI |
| 应用独立进程 | 2 | 两次实际进程启动、损坏文件失败日志 |
| 基础设施防护 | 4 | 缺失/多 head 拒绝、目录创建失败、离线 SQL |

迁移/事务测试的所有文件均位于临时目录；正式 Base.metadata 始终为空。测试事务表用独立 MetaData，不修改正式 schema。

## 非离屏窗口与 CLI 验收

在另一个自动清理的临时目录使用同一 TOML，执行：

1. CLI upgrade head。
2. CLI current：`0001_empty_initial (head)`。
3. CLI history：`<base> -> 0001_empty_initial (head)`。
4. CLI downgrade base。
5. CLI 再次 upgrade head。
6. 正常 Windows Qt 平台启动应用两次，各自显示窗口并完成导航检查后退出。
7. 再次打开数据库，schema 为 `['alembic_version']`，版本为 `('0001_empty_initial',)`。

两次窗口日志都确认 `Qt platform=windows`，不是 offscreen；退出码都是 0，error.log 为空。CLI 从非项目 cwd 使用绝对 alembic.ini 路径运行，未生成另一份数据库。

## 已发现并修正的问题

- Phase 1 历史权限阻塞在本次完全权限下不再影响基础测试。
- 系统 conda 包目录不可写：改用局部虚拟环境，不申请管理员权限、不修改 Windows 文件权限。
- 已配置的 pip 镜像没有返回可用 Alembic：改用官方 PyPI 安装缺失包。
- Alembic `prepend_sys_path=.` 在独立 cwd 无法导入 app：改为 `%(here)s` 并通过两个 cwd 下的 current/history 测试。
- 默认日志原来在源码目录：迁入统一用户数据目录，显式旧日志配置仍兼容。

## 验证边界 / 尚未宣称支持

- 仅实测当前 Windows/Python 环境，未在其他 Python 小版本或 Linux/macOS 上执行全套测试。
- 未做断电、磁盘满、真实网络文件系统和崩溃恢复故障注入。
- 未对所有继承 Session API 做跨线程保护；支持的是严格禁止跨线程的使用约定及常用 API 检查，不能据此传递 Session/ORM 对象。
- 未完成 wheel/exe 迁移资源分发、备份恢复实现和多实例同时首次初始化策略；见 [数据库文档](<database.md>)。
- 原生窗口启动/导航检查不等同于所有屏幕 DPI 和缩放比例的人工 UI 验证。
- 没有业务实体、假业务数据、LLM、Graph 数据逻辑或同步。
