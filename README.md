# 乡镇预警服务差距的多口径复盘

这些演练台账是本地合成资料，使用虚构编号。CSV 采用 UTF-8 和带表头的逗号分隔格式。同一单元内用竖线列出的值表示多项或候选集合，含义见各表说明。空值通常表示未记录或不适用，明确的零会保留为 0。

## 资料

- `data/people.csv`  人群归属候选。responsibility 为服务安排，已停用的接收点有 stop 记录。joined 为生效季，responsibility 未变时继续处于指标范围。
  字段为 `group / kind / village_candidates / low / high / joined / stop / responsibility`。
- `data/standards.csv`  面向不同用途的参考权重。协议可在两份参考群体都支持的地方给出稳健判断。
  字段为 `standard / kind / risk / weight`。
- `data/channels.csv`  服务责任和实际接收渠道有效范围。common_point 指共同接收点。
  字段为 `season / village / channel / common_point`。
- `data/budget.csv`  可用改进额度和已有承诺。share_limit 是共同服务点的额度，lowest 是必须优先补足的最低覆盖水平。
  字段为 `season / village / total / locked / share_limit / lowest`。
- `data/appeals.csv`  乡镇申诉补交归属事实。only_new_season 表示变更是否只用于当前季。
  字段为 `id / group / village / reported_season / only_new_season`。
- `data/weather.csv`  经独立确认的风险机会和各自的送达时限。
  字段为 `season / storm / village / risk / state / deadline_min`。
- `data/reach.csv`  接收事实和能适用的候选群组。proof 相同是同一份证据，不意味着各自人数都可累加。count 为空代表未知。
  字段为 `storm / village / group_options / state / count / proof`。

## 核对台账

环境为 Python 3.11.2，只使用标准库。在仓库根目录运行下列命令检查文件格式、表头和基础外键。输出为各张源表可读行数。

```sh
python3 input_audit.py
python3 -m unittest discover -s tests -v
```

可在第一条命令后指定另一个输入目录，目录中的同名 CSV 沿用上述格式。列名和基础引用清单见 `input_contract.json`。

## 服务公平复算工具

`equity` 包把各季台账复算成可比的公平分析与下一季改进额度。在仓库根目录运行，全部命令输出 JSON：

```sh
python3 -m equity --data data audit                      # 格式 + 语义核对
python3 -m equity report [--season C] [--scenario baseline|appealed]
python3 -m equity allocate [--season C] [--scenario ...] # 默认取额度台账最后一季
python3 -m equity appeals                                # 申诉重算对照（基线 vs 计入申诉）
python3 -m equity query --season C [--village T3 | --group P6]
```

### 复算口径

- **分层实测**：按 季×分区×风险层 汇总确认危险过程与触达记录。及时率点估计只用可信记录（按时/超时）；未知记录不进入点估计，但进入区间——全部按超时计为下界、全部按按时计为上界。无危险过程的分层不进入比较也不算失败；有危险但无可信记录的分层按 [0, 1] 区间处理并单独列出后续动作。
- **指标范围**：群组自生效季起进入范围；接收点季中停用但服务责任未变时继续计入，不剔除对应人群。
- **归属一致**：基线取候选首项，申诉事实按 only_new_season 决定适用季。全县汇总只在单一归属情景内计算，跨情景只给敏感范围，保证任何一张全县表都对应一种真实可能的归属解释。
- **标准人口**：PRESENT 与 PROTECT 两套参考权重分别标准化；结论需在两套权重、全部一致归属情景与区间端点下都成立才算"稳健"，否则只作局部观察。固定户与流动聚居点精度不同，严格比较只用 low 人口（可证明的共同部分），其余以区间给出。
- **实际覆盖**：观测计数原样保留；相同 proof 为同一份证据，跨分区人数不可直接累加，合计按 proof 去重。
- **额度**：已承诺保障（locked）优先保留；新增额度只覆盖"确有缺口"（两套标准人口下区间上限仍低于最低覆盖水平的部分），"可能缺口"列为待补证额度。可核准额度按去重归属人口计算，不按各图表下降幅度相加，并受 total-locked 与共享接收点 share_limit 约束。申诉重算时证据不完整的分区核准额度只增不减。
- **回查**：`query` 给出指定季与分区的指标范围、各分层适用范围、额度变动与依据。
