# 🧬 mutest

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

**变异测试驱动的 AI 测试生成闭环 —— [qodo-cover](https://github.com/qodo-ai/qodo-cover)（2025 年 6 月已停止维护）的 MIT 协议继任者。**

行覆盖率是个虚荣指标。`mutest` 往你的代码里注入微小的 bug（变异体），让你的测试套件逐个去抓，按抓获率打分：**变异分数 = 杀死数 / 总数**。没抓到的"幸存者"，让 LLM 针对性地写测试来补——再用*另一个*模型交叉验证，并且实打实跑一遍确认。超时和执行错误会保留在总数中，并报告为未杀死。

## ⚡ 30 秒演示（真实输出）

```text
$ mutest --src examples/sample_calc --tests examples/sample_calc --no-llm
generated 20 mutants from 1 file(s) (10 operators)
running baseline test suite...
baseline green ✓
mutation score: 70.0% (14 killed / 6 survived / 20 total, 7s)
report written to mutest-report.html
```

同一个项目，两个指标：

| 指标 | 分数 | 含义 |
|---|---|---|
| 行覆盖率 | **93%** | 面板上很好看 |
| **变异分数（mutest）** | **70%** | **30% 注入的 bug 完全没被发现** |

比如这个幸存者——没人测过的边界：

```diff
# calc.py:14  (ROR: >= -> >)
-    return age >= 18
+    return age > 18     # is_adult(18) 现在会错误返回 False，但测试套件一言不发
```

完整 HTML 报告：[`examples/sample_calc/report.html`](examples/sample_calc/report.html)

## 🚀 快速开始

```bash
pip install mutest        # 只打分，安装保持轻量
pip install "mutest[ai]"  # 使用 --model 时再安装可选的 litellm
# v0.1.0 发布到 PyPI 前，也可以使用：
pip install "mutest @ git+https://github.com/chenyi6758-source/mutest"
pip install "mutest[ai] @ git+https://github.com/chenyi6758-source/mutest"

cd your-python-project
mutest --src src --tests tests --no-llm              # 只打分，不需要 API key
mutest --src src --tests tests --model gpt-4o-mini   # 需要先安装 mutest[ai]
```

要求：Python 3.10+，`pytest` 套件本身是绿的（基线飘红时 mutest 会直接拒绝运行）。`litellm` 是可选依赖：只有使用 `--model` 或 `--verify-model` 时才需安装 `mutest[ai]`。

加上 `--model` 后，mutest 会挑变异分数最低的幸存者，让 LLM 逐个写针对性测试，写入 `tests/test_mutest_gen.py`，然后**实证验证**：新测试必须在原始代码上通过、在变异体上失败。只有真杀死了变异体，分数才会涨。

## 🧠 工作原理

1. **变异** —— 10 个 AST 算子（AOR/ROR/LOR/NOT/NEGCOND/NUM/STR/RETNONE/RETBOOL/UNARY），每个变异点生成一个变异体。每个变异体都会重新 parse，无效代码和重复变异体自动丢弃。
2. **处决** —— 每个变异体在隔离的临时工作区里跑完整 `pytest` 套件，并行执行（`-x`：第一次失败就停，我们只关心"抓没抓住"）。
3. **生成** —— 对最顽固的幸存者，LLM 写针对性测试（`--model`，需安装 `mutest[ai]`；litellm 支持的都行；key 走标准环境变量，不落盘）。
4. **交叉验证** —— 可选 `--verify-model`（换一个厂商的模型）评审生成的测试，再实跑确认：在原始代码上通过、在变异体上失败。
5. **报告** —— 单文件 HTML：分数卡片、分文件明细、每个幸存者的原代码/变异代码 diff、生成的测试。

## 🆚 mutest vs qodo-cover

| | mutest | qodo-cover |
|---|---|---|
| 状态 | ✅ 维护中 | ❌ 已停止维护（2025-06） |
| 协议 | **MIT**（企业友好） | AGPL-3.0（很多公司的 SBOM 直接 ban） |
| 质量指标 | **变异分数** | 行覆盖率 |
| AI 测试闭环 | 有，带跨模型验证 | 有 |
| 等价变异体 | 手动标记（自动判定是 P2） | n/a |

## ⚙️ 常用参数

```
--model gpt-4o-mini              生成测试用的 LLM（litellm 格式随便写）
--verify-model claude-haiku-4-5  第二个模型交叉评审，打破"自己考自己"
--top-n 10                       针对多少个幸存者生成测试
--workers 4 / --timeout 120     （自动 worker 最多 8 个；显式值不受上限限制）
--fail-under 75                 低于 75 个百分点时让 CI 失败
--incremental                    只变异 git HEAD 改动过的文件（PR 场景）
--base-ref BASE                   PR 基准版本；配合 --incremental 比较 BASE...HEAD
--json                           输出 {"score": ...} 给 CI 用
--config mutest.yaml             算子开关等（见 src/mutest/config.py）
```

环境变量：`MUTEST_MODEL`，`MUTEST_LLM_PROVIDER=fake`（离线演示/测试用）。

## 🤖 GitHub Action

```yaml
- uses: chenyi6758-source/mutest@main  # 生产环境请固定到具体提交 SHA
  with:
    src: src
    tests: tests
    # fail-under: "75"      # 设置后超时或执行错误也会失败
    incremental: "true"
    base-ref: ${{ github.event.pull_request.base.sha }}
```

详见 [`action.yml`](action.yml) 和 [`.github/workflows/mutest-example.yml`](.github/workflows/mutest-example.yml)（示例：在每个 PR 下自动评论变异分数）。

## ❓ FAQ

**分数低，是我的代码烂吗？**
不一定——低分说明你的*测试*抓不住行为变化。这正是行覆盖率永远不会告诉你的信息。

**等价变异体（不改变行为的变异）会被判"幸存"，怎么办？**
看 HTML 报告确认它确实等价，加到排除列表。等价性判定一般情况下不可判定，这是 P2 的路线图，v0.1 不做。

**我的仓库跑起来很慢。**
用 `--incremental`（只跑改动的文件）、`--workers N`，或在 `mutest.yaml` 里关掉部分算子。变异测试本质是 O(变异体数 × 套件时间)，并行执行 + `-x` 提前退出已经把它压到实用区间。

**生成的测试会联网吗？**
生成的测试在本地执行，运行前会移除类似 secret、token、key、password 的环境变量，但这不是完整的网络沙箱。因此应把生成代码视为不可信代码，只在适当隔离的 CI 环境中运行。mutest 自身只有在你用 `--model` 时才会通过 litellm 直连模型厂商；key 不会写入仓库。

## 🗺️ 路线图

- [x] v0.1：10 个 AST 算子、并行执行器、LLM 闭环、HTML 报告、GitHub Action
- [ ] P2：等价变异体聚类、JS/TS 支持、flaky 测试检测、Ollama/本地模型

## 🤝 参与贡献

欢迎 PR——新变异算子和真实项目的分数报告尤其欢迎。推送前跑 `pytest`，必须全绿。

---

如果 mutest 抓到了你的覆盖率徽章没抓到的 bug，请给个 ⭐ ——这是项目活下去的燃料。

[English README](README.md)
