#商品目录变更入库交付

sql目录保存建表、批次判决和批次调用SQL。results目录保存目录全量状态、可用目录、事件决策、批次回执、装载回执和处理摘要。

目录运维先核对raw_load_receipt.csv中的接收情况，再用decision_ledger.csv说明同批落选和迟到事件。catalog_state.csv保留删除水位，active_catalog.csv供目录查询服务装载，batch_receipts.csv用于交班对账。

cdc_summary.json汇总数据库版本、输入规模、决策分布和最终目录规模。SQL与结果一起交给目录数据组留档，后续批次沿用同一位置比较和墓碑规则。
