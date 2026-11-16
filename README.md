# 社区健身赛事数据服务

本项目为社区健身活动提供从报名、评分、现场签到、便民服务记录到成果发布的 FastAPI 接口。业务数据保存在本地 SQLite；演示样本均为合成数据，不代表真实参赛者或活动统计。

## 启动与验证

在已安装 requirements.txt 锁定版本的 Python 环境中运行：

    uvicorn community_fitness.api:app --host 127.0.0.1 --port 8000

可设置 FITNESS_DB 环境变量选择数据库文件。接口说明位于 /docs。以下命令分别运行真实业务测试、语法检查和完整合成活动演示：

    python -m unittest discover -s tests -v
    python -m compileall -q community_fitness
    python -m community_fitness.demo

## 数据流程

在草稿阶段建立赛事、评分组、评分规则、队伍、裁判和服务专区。赛事开启后，每个组单独开启评分会话。裁判以请求键提交逐项分数；卡片修订使用预期旧版，并保留历史。只有组内全部有效裁判完成评分，队伍才参与排名。排名相同者共享名次；缺失评分不会当作零分。

现场签到以队伍内匿名成员标识去重，不能超过报名人数。专区记录分别保存接触次数、服务人次和匿名访客标识。批量导入逐行报告接收或拒绝结果，重复请求安全重放，同一记录 ID 的不同内容报告冲突。时间均须带明确偏移，专区接触必须落在开放区间内。

评分争议未解决时，组别评分会话无法关闭。所有会话关闭后才能关闭赛事。预览给出评分完整性、未决争议、服务统计和源修订号。发布时同时提交预期旧版与源修订号，防止并发更新覆盖结果。已发布版本保持不可变；迟到的服务记录可形成新版，但旧版查询及 CSV 导出继续返回原结果。

对账、准备状态、溯源、审计、分时统计与质量接口用于定位缺报、签到与服务记录。fixtures/ 中的本地样本仅用于演示与测试。

## 报名-到场-评分-获奖转化漏斗

`GET /events/{id}/funnel/live` 与 `GET /events/{id}/funnel/snapshot` 按区、按组别返回四个阶段的转化分析，每个阶段都显式给出分母，缺报队伍附带机器可读原因码（`reason_legend` 含中文释义）：

| 阶段 | 分子 | 分母 |
| --- | --- | --- |
| 报名 registered | 全部报名队伍 / 成员 | teams 表（草稿期锁定），队伍与成员双口径 |
| 到场 attended | 至少 1 人签到的队伍 / 去重签到成员 | 报名队伍数 / 报名成员数（各队 member_count 求和） |
| 完成评分 scored | 评分已定案的队伍：裁判配置达标、评分卡齐全、无未决争议且评分会话已关闭 | **到场队伍数**；未到场队伍记 `not_scored_no_show` 单列，不进分母，避免被误判为表现差。仅卡片齐全但会话未关闭/有争议的队伍计入 `completed_card_teams`，原因码为 `session_not_closed`/`open_dispute` |
| 获奖 awarded | 组内 `rank <= award_top_n`（同分并列共享名次；未定案队伍不显示名次） | 完成评分队伍数 |

口径约定：

- 队伍人数不等，因此队伍口径与成员口径并行；到场率同时给出 `team_attendance_rate` 与 `member_attendance_rate`。
- 签到按 `(event_id, team_id, member_ref)` 主键去重，同 `request_key` 重试只做安全重放，不会虚增到场人数。
- 赛事关闭后的赛后补记仍然允许（attendance 在 open/closed 状态均可写入），但只影响此后的新版本。
- **live 与 snapshot 不可混拼**：实时视图反映当前表（含未关闭会话 `session_not_closed`、未决争议 `open_dispute` 等原因），快照视图来自不可变发布版本。每条响应带 `basis`、`version`、`source_revision` 与 `comparison_key`；CSV 导出（`/events/{id}/export/funnel.csv`）默认只导快照，传 `basis=live` 需显式指定，导出的每行同样带 basis/version。
- 分母为零时比率返回 `null` 而非 `0`，与真实的零转化区分；另附 `score_completion_rate_of_registered` 作为以报名为底的参考口径。
