"""验证用查询与预期命中。README 展示页与 scripts/verify_search.py 共用。"""

EXPECTED_QUERIES = [
    {
        "query": "采样窗口",
        "must_have": {
            "运动传感器时序分类实验报告 A": ["采样窗口"],
            "可穿戴动作识别复现报告 B": ["采样窗口"],
        },
        "note": "同名段落出现在两份报告中，应同时命中且可区分来源",
    },
    {
        "query": "macro F1",
        "must_have": {
            "运动传感器时序分类实验报告 A": ["类别不平衡", "实验结果"],
            "可穿戴动作识别复现报告 B": ["类别不平衡"],
        },
        "note": "中英混排检索，命中含 macro F1 的段落",
    },
    {
        "query": "length alignment",
        "must_have": {
            "Reading notes (English)": ["Length alignment"],
            "运动传感器时序分类实验报告 A": ["长度对齐"],
        },
        "note": "中文报告的正文含英文术语，也应被英文查询命中",
    },
    {
        "query": "紫色独角兽观测 zz-qux-unicornium",
        "expect_empty": True,
        "note": "所有词均不在库内，界面应显示空态而不是报错",
    },
]

FILTERED_QUERIES = [
    {
        "query": "采样窗口",
        "report": "运动传感器时序分类实验报告 A",
        "must_have_titles": ["运动传感器时序分类实验报告 A"],
        "forbid_titles": ["可穿戴动作识别复现报告 B"],
        "note": "按报告筛选后只保留该报告的段落",
    },
    {
        "query": "类别不平衡",
        "tag": "复现实验",
        "must_have_titles": ["可穿戴动作识别复现报告 B"],
        "forbid_titles": ["运动传感器时序分类实验报告 A"],
        "note": "按标签筛选只命中带该标签的报告",
    },
]
