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
