# Day 7 真实测试快速指南

## ✅ 前置条件

Day 6 已完成：
- [x] RecoveryManager 已集成到 DataViewerWorkflow
- [x] 智谱AI支持已添加
- [x] 78个单元测试全部通过
- [x] API Key已配置

---

## 🚀 快速开始

### 步骤1：配置环境变量

编辑 `.env` 文件（或创建 `.env.local`）：

```bash
# ==================== AI Exception Recovery ====================
ENABLE_AI_RECOVERY=true
AI_RECOVERY_PROVIDER=zhipuai
AI_RECOVERY_MODEL=glm-4-plus
ZHIPUAI_API_KEY=e85242ac28f54929b44e58daf49bebb9.Yn7CtzeLGzBAR9s6

# Recovery behavior
AI_RECOVERY_CONFIDENCE=0.7
AI_RECOVERY_MAX_RETRIES=2
AI_RECOVERY_MAX_CALLS=3

# Logging
AI_RECOVERY_LOG_DECISIONS=true
AI_RECOVERY_LOG_DIR=logs/ai_recovery
```

### 步骤2：验证配置

```bash
# 测试配置是否正确
python -c "
from src.recovery import AIRecoveryConfig

config = AIRecoveryConfig.from_env()
print(f'Provider: {config.provider}')
print(f'Model: {config.model}')
print(f'Enabled: {config.enabled}')
print(f'API Key: {config.api_key[:10]}...')

errors = config.validate()
if not errors:
    print('✅ Config valid!')
else:
    print(f'❌ Errors: {errors}')
"
```

**预期输出**:
```
Provider: zhipuai
Model: glm-4-plus
Enabled: True
API Key: e85242ac28...
✅ Config valid!
```

### 步骤3：启动测试脚本

创建测试脚本 `test_real_recovery.py`:

```python
"""
Day 7 真实测试脚本

测试AI恢复系统与真实GDS2的集成。
"""

import logging
from src.workflows.data_viewer import DataViewerWorkflow

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def test_normal_workflow():
    """测试1：正常流程（无异常）"""
    print("\n" + "="*70)
    print("测试1：正常流程（无异常）")
    print("="*70)

    workflow = DataViewerWorkflow(enable_ai_recovery=True)

    if not workflow.recovery:
        print("❌ AI recovery未启用，检查配置")
        return False

    print(f"✅ AI recovery已启用")
    print(f"   Provider: {workflow.recovery.config.provider}")
    print(f"   Model: {workflow.recovery.config.model}")

    try:
        # 启动workflow
        print("\n1. 启动workflow...")
        result = workflow.start()

        if "devices" in result:
            print(f"✅ 检测到设备列表：{result['devices']}")
            print("\n请选择一个设备并调用 workflow.connect_device()")
        elif "modules" in result:
            print(f"✅ 已连接设备，检测到 {len(result['modules'])} 个模块")
            print(f"   VIN: {result.get('vin', 'N/A')}")

        # 检查AI调用次数
        if workflow.recovery:
            print(f"\n📊 LLM调用次数: {workflow.recovery.llm_call_count}")

        return True

    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_with_simulated_timeout():
    """测试2：模拟超时场景（需要手动触发）"""
    print("\n" + "="*70)
    print("测试2：超时场景")
    print("="*70)
    print("\n⚠️  手动测试步骤：")
    print("   1. 拔掉VCI设备")
    print("   2. 调用 workflow.connect_device('SM2 USB')")
    print("   3. 观察AI是否检测到超时并恢复")
    print("   4. 检查 logs/ai_recovery/decisions.jsonl")


def test_with_simulated_dialog():
    """测试3：模拟弹窗场景（需要手动触发）"""
    print("\n" + "="*70)
    print("测试3：弹窗场景")
    print("="*70)
    print("\n⚠️  手动测试步骤：")
    print("   1. 在GDS2操作过程中触发错误对话框")
    print("   2. 观察AI是否检测到弹窗")
    print("   3. AI应自动点击OK/确定按钮")
    print("   4. 检查 logs/ai_recovery/decisions.jsonl")


def check_decision_logs():
    """检查AI决策日志"""
    print("\n" + "="*70)
    print("检查AI决策日志")
    print("="*70)

    from pathlib import Path
    import json

    log_file = Path("logs/ai_recovery/decisions.jsonl")

    if not log_file.exists():
        print("ℹ️  还没有决策日志")
        return

    print(f"\n📁 日志文件: {log_file}")

    with open(log_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    print(f"📊 总决策数: {len(lines)}")

    if lines:
        print("\n最近5条决策：")
        for line in lines[-5:]:
            try:
                entry = json.loads(line)
                print(f"\n  时间: {entry['timestamp']}")
                print(f"  异常: {entry['anomaly']['type']}")
                print(f"  决策: {entry['decision']['action']}")
                print(f"  置信度: {entry['decision']['confidence']:.2f}")
                print(f"  原因: {entry['decision']['reasoning']}")
            except:
                pass


if __name__ == "__main__":
    print("\n🎯 Day 7 - 真实测试")
    print("AI异常恢复系统 + 真实GDS2 + 智谱AI GLM-4-Plus")
    print()

    # 测试1：正常流程
    success = test_normal_workflow()

    if success:
        print("\n✅ 基础功能测试通过！")

        # 显示手动测试说明
        test_with_simulated_timeout()
        test_with_simulated_dialog()

        # 检查决策日志
        check_decision_logs()

        print("\n" + "="*70)
        print("📝 测试清单")
        print("="*70)
        print("  [ ] 正常流程（无异常）")
        print("  [ ] 超时恢复（拔VCI设备）")
        print("  [ ] 弹窗恢复（触发错误对话框）")
        print("  [ ] 检查决策日志")
        print("  [ ] 验证成本（检查API调用次数）")
        print()
```

### 步骤4：运行测试

```bash
# 确保GDS2已启动
python test_real_recovery.py
```

---

## 📋 测试场景

### 场景1：正常流程（基准测试）
**目标**: 验证AI恢复不影响正常流程

**步骤**:
1. 启动GDS2
2. 确保VCI设备已连接
3. 运行测试脚本
4. 验证workflow正常工作

**预期**:
- ✅ Workflow正常运行
- ✅ LLM调用次数 = 0（无异常）
- ✅ 无恢复操作

---

### 场景2：超时恢复
**目标**: 测试AI处理超时异常

**步骤**:
1. 启动workflow
2. **在等待Enter按钮时拔掉VCI设备**
3. 观察日志输出
4. 检查 `logs/ai_recovery/decisions.jsonl`

**预期**:
- ✅ AI检测到超时
- ✅ AI决策：WAIT_LONGER 或 ABORT
- ✅ 日志记录完整
- ✅ LLM调用次数 = 1

**验证**:
```bash
# 查看决策日志
tail -5 logs/ai_recovery/decisions.jsonl | jq
```

---

### 场景3：弹窗恢复
**目标**: 测试AI处理意外对话框

**步骤**:
1. 启动workflow
2. **触发GDS2错误对话框**（例如：断网、VCI断开）
3. 观察AI是否自动点击OK
4. 检查决策日志

**预期**:
- ✅ AI检测到弹窗
- ✅ AI决策：CLICK_BUTTON("OK")
- ✅ 弹窗自动关闭
- ✅ Workflow继续运行

---

## 🔍 监控和调试

### 实时查看日志

```bash
# 终端1：运行workflow
python test_real_recovery.py

# 终端2：实时查看AI决策日志
tail -f logs/ai_recovery/decisions.jsonl | jq

# 终端3：查看Python日志
# (如果启用了文件日志)
tail -f logs/workflow.log
```

### 检查API调用

```python
# 在workflow中检查
if workflow.recovery:
    print(f"LLM calls: {workflow.recovery.llm_call_count}")
    print(f"Max allowed: {workflow.recovery.config.max_llm_calls_per_session}")
```

### 查看决策详情

```bash
# 查看最后一条决策的完整信息
tail -1 logs/ai_recovery/decisions.jsonl | jq

# 提取关键信息
tail -10 logs/ai_recovery/decisions.jsonl | jq '.decision | {action, confidence, reasoning}'
```

---

## 📊 成功标准

Day 7 测试通过的标准：

### 必须达标 (Must Have)
- [x] 配置加载成功
- [ ] 正常流程工作（无回归）
- [ ] AI检测到至少1个异常
- [ ] AI成功恢复至少1次
- [ ] 决策日志正确生成
- [ ] LLM调用在限制内（≤3次/session）

### 应该达标 (Should Have)
- [ ] 恢复成功率 ≥80%
- [ ] 单次恢复成本 <$0.02
- [ ] 恢复时间 <10秒
- [ ] 无无限递归
- [ ] 无异常崩溃

### 可选达标 (Nice to Have)
- [ ] 恢复成功率 ≥90%
- [ ] 单次恢复成本 <$0.01
- [ ] 恢复时间 <5秒

---

## 🐛 常见问题

### 问题1：AI recovery未启用

**症状**: `workflow.recovery is None`

**解决**:
```bash
# 检查环境变量
python -c "import os; print(os.getenv('ENABLE_AI_RECOVERY'))"

# 检查API key
python -c "import os; print(os.getenv('ZHIPUAI_API_KEY')[:10])"

# 验证配置
python -c "
from src.recovery import AIRecoveryConfig
config = AIRecoveryConfig.from_env()
print(config.validate())
"
```

### 问题2：ZhipuAI API调用失败

**症状**: `zhipuai.APIError` 或类似错误

**解决**:
```python
# 测试API连接
from zhipuai import ZhipuAI

client = ZhipuAI(api_key="your_key")
response = client.chat.completions.create(
    model="glm-4-plus",
    messages=[{"role": "user", "content": "测试"}],
)
print(response.choices[0].message.content)
```

### 问题3：决策日志不生成

**症状**: `logs/ai_recovery/decisions.jsonl` 不存在

**原因**:
1. `AI_RECOVERY_LOG_DECISIONS=false`
2. 没有触发任何异常

**解决**:
```bash
# 启用日志
export AI_RECOVERY_LOG_DECISIONS=true

# 手动触发异常（拔VCI设备）
```

---

## 📈 性能分析

测试完成后，分析性能指标：

```python
# 统计脚本
import json
from pathlib import Path
from collections import Counter

log_file = Path("logs/ai_recovery/decisions.jsonl")
if log_file.exists():
    with open(log_file) as f:
        decisions = [json.loads(line) for line in f]

    # 异常类型分布
    anomaly_types = Counter(d['anomaly']['type'] for d in decisions)
    print("异常类型:", dict(anomaly_types))

    # 决策动作分布
    actions = Counter(d['decision']['action'] for d in decisions)
    print("决策动作:", dict(actions))

    # 平均置信度
    avg_confidence = sum(d['decision']['confidence'] for d in decisions) / len(decisions)
    print(f"平均置信度: {avg_confidence:.2f}")

    # 总LLM调用
    total_calls = sum(d['llm_call_count'] for d in decisions)
    print(f"总LLM调用: {total_calls}")
```

---

## ✅ 完成检查清单

Day 7 测试完成后，确认以下事项：

- [ ] 配置文件正确 (`.env`)
- [ ] 正常流程测试通过
- [ ] 触发并恢复至少1个异常
- [ ] 决策日志已生成
- [ ] 成本在预算内
- [ ] 没有崩溃或错误
- [ ] 性能符合预期

---

**准备好开始 Day 7 测试了吗？**

运行测试脚本：
```bash
python test_real_recovery.py
```

或者先验证配置：
```bash
python -c "from src.recovery import AIRecoveryConfig; print(AIRecoveryConfig.from_env())"
```
