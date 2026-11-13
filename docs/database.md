# 数据库基础设施（Phase 2–5）

## 位置与统一配置

[配置](<../app/config/settings.py>) 和 [路径解析](<../app/config/paths.py>) 共用于应用和 Alembic。默认 platformdirs 用户数据根目录；支持绝对 data_dir 或相对 TOML 文件所在目录的路径。database 只能是文件名。默认日志在 data_dir/logs，显式日志目录仍按配置文件解析。正式数据库与 media 位于同一根目录，源码不要求写入。

## Engine / Session / 事务

[Engine](<../app/storage/database.py>) 每个应用生命周期创建一次；新连接统一启用 foreign_keys=ON、journal_mode=WAL、busy_timeout（默认5000ms）。外键是每连接设置；WAL改善读写并行，但仍只有一个写者，不能据此共享 Session。busy_timeout 只限定锁等待，不替代业务重试或文件锁。

SQLite DBAPI 的 legacy 自动事务行为关闭，begin事件显式执行：

- 普通 `transaction(factory)`：BEGIN，读事务保留快照语义。
- `transaction(factory, write=True)` / `write_operation(factory)`：BEGIN IMMEDIATE，在首次业务读取前预留写锁。

SQLite SQL 和 DBAPI 操作仅在 Storage 适配器内；Service 传递写意图，不直接执行 SQLite SQL。未来 PostgreSQL Engine 不注册 SQLite begin事件，写意图标记可由其他适配器处理。

[SessionFactory](<../app/storage/session.py>) 每次 operation 创建独立 Session。成功由 begin 上下文提交，失败回滚，最后关闭；关闭后不可复用。工厂/Engine 可复用，Session、Connection、ORM lazy对象不跨线程传递。常用 Session API 检查线程，不能将检查器当作任意跨线程访问的支持承诺。

Repository 接收当前 Session，save 可以 flush 以确保外键次序；**不 commit、rollback 或创建 Session/Engine**。返回纯 Domain 快照，UI 不拿 ORM object。事务测试表使用独立 MetaData，不污染正式九表。

## 模型、类型和约束

[正式模型](<../app/storage/models.py>)：workspace、person、person_alias、media、person_media、relationship、relationship_history、event、event_participant、evidence、evidence_media。Base 使用统一命名规则；PK/FK/UNIQUE/INDEX/CHECK 名称稳定。正式 schema 不通过 create_all 生成。

Domain UUID 映射 SQLAlchemy Uuid。Domain 时间只接受 aware datetime；[UTCDateTime](<../app/storage/types.py>) 绑定时统一转 UTC，SQLite 保存 UTC 墙钟值并在读取恢复 UTC tzinfo，PostgreSQL 使用 timezone-aware timestamp。naive datetime 被拒绝；birthday 是 date。

- Person / Media → Workspace FK。
- PersonAlias(workspace_id,person_id) → Person(workspace_id,id)。
- PersonMedia 两组复合外键分别指向 Person 和 Media，数据库直接拒绝跨 Workspace 引用。
- Media UNIQUE(workspace_id,sha256)，包括已删除记录。
- PersonMedia UNIQUE(person_id,media_id,role)。
- partial UNIQUE(person_id) WHERE role='AVATAR'（SQLite和PostgreSQL）。
- display_name/real_name/alias 不唯一；普通 scope/name/alias 索引用于当前搜索，没有 FTS。

数据库不表达全部生命周期规则：父对象必须未归档、删除 Media 前无有效引用、Workspace 中无有效 Person/Media 才可归档，由 Service 在预留写事务内验证。路径 CHECK 只做基础相对路径限制，完整 canonical 媒体路径与链接防护由 Domain 和 MediaStore 验证。关系端点/History使用复合FK，身份和状态契约见 [关系文档](<relationships.md>)；原子 Event 与参与人操作、archived 历史保留、日期／时间二选一 CHECK 与 History 可空复合 Event FK，见 [事件文档](<events.md>)。Evidence 只挂在 Event 上、原始内容只读、来源与注释分离，见 [证据文档](<evidence.md>)。Event 不自动产生关系状态；History 仅 event_id 关联元数据允许受控修改。Analysis 等后续模型未实现。

## 迁移与启动

[迁移配置](<../alembic.ini>) 没有硬编码 URL；env 通过同一 AppConfig 创建 Engine、加载九模型 metadata。历史 revision：

```text
0001_empty_initial → 0002_workspace_person_media → 0003_relationship → 0004_event → 0005_evidence → 0006_search_projection → 0007_analysis → 0008_memory
```

0001 只有版本表；0002 建立五张业务表；0003新增关系与History两表；0004新增Event／Participant并batch扩展History.event_id；0005新增Evidence／EvidenceMedia两表，达到十一张业务表；**0006 只新增搜索投影基础设施**（`search_document` 普通表 + `search_document_fts` FTS5 虚表），不新增业务事实表、不改事实表结构；**0007 新增分析层 5 张表**（analysis_run 与 person／relationship／event／evidence 四张来源关联表）；**0008 只新增 `memory` 一张表**（用户确认的长期记忆，复合 FK 保证不跨 Workspace 引用 Person 或 AnalysisRun）。三者都不动事实表。旧迁移未修改。离线SQLite SQL仅使用迁移内冻结的History定义作copy_from，在线batch仍反射，保留原列与命名约束。Default Workspace 不在迁移中插入，而由应用装配后的 WorkspaceService 幂等创建。

```powershell
python -m alembic upgrade head
python -m alembic current
python -m alembic history
python -m alembic -x config=C:\config\runtime.toml upgrade head
```

外部 cwd 指定 `-c <绝对项目路径>\alembic.ini`；脚本路径与导入路径基于 ini，不跟随 cwd。离线 `upgrade head --sql` 不连接数据库。

空库自动迁移；有未知表但无版本的库拒绝接管；旧/未知 revision 拒绝启动，需显式迁移；缺失/多 head 脚本拒绝初始化。失败清理Engine，正常窗口关闭dispose。源码/可编辑安装已验证；wheel/exe迁移资源分发仍待完成。

**downgrade 0007 丢弃分析层 5 张表；downgrade 0006 只丢弃可重建的搜索投影；downgrade 0004 会丢弃 Event／Participant／event_id 引用，downgrade 0005 会丢弃 Evidence／EvidenceMedia，但原七表事实保留；downgrade 0002 会删除关系与History；降到0001还会删除原五表，不是恢复备份！** 测试仅用临时库，保留旧循环，并增加seeded 0003→0004→0003→0004、seeded 0004→0005→0004→0005、0005→0006→0005→0006、0006→0007→0006→0007、迁移失败整体回滚、offline SQL真实执行往返和Alembic command.check。

**SQLite 临时目录：** 创建与删除 FTS5 虚表需要可写临时目录，否则 SQLite 报 `SQLITE_CANTOPEN`。受限宿主（本仓库开发环境）通过 `SQLITE_TMPDIR`／`TMPDIR`／`TEMP`／`TMP` 指向可写目录解决，见 [conftest.py](<../conftest.py>)。

## 错误、隐私与测试

数据库错误进入现有 app/error.log，包含操作、异常类和驱动代码，不输出 SQL、参数、私人字段或原始异常字符串。Engine hide_parameters=True、echo=False；UI从应用错误接口获取可理解提示，普通日志不序列化实体。只读/目录/损坏/锁冲突分别有测试；Windows只读属性不等同Unix mode，未在其他OS宣称实测。

```powershell
python -m pytest tests/database tests/storage tests/migrations -q
```

涵盖 PRAGMA、WAL、锁等待、线程/Session隔离、事务整体回滚、读写BEGIN模式无池泄漏、复合FK、部分唯一索引、UTC重载、迁移反射一致性和应用重启。PostgreSQL表和索引DDL已编译验证，但未运行真实PostgreSQL服务。没有 SQLite 自增业务ID；未来 ChangeLog/revision 可和业务操作同事务加入，本阶段没有这些表或服务。

**当前 backup 尚未实现。** WAL运行时不能只复制主库认为是一致备份；数据库+media必须统一备份，详见 [媒体文档](<media.md>)。
